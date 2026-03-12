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
     Find your chat_id in the output and add to .env:
     TELEGRAM_CHAT_ID=<your chat_id>
  4. pip install python-telegram-bot
  5. python run_edge_bot.py

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

_kalshi_api   = importlib.import_module(".dat-ingestion.kalshi_api", "edge_agent")
_injury_mod   = importlib.import_module(".dat-ingestion.injury_api", "edge_agent")
_InjuryClient = _injury_mod.InjuryAPIClient

_trader_mod   = importlib.import_module(".dat-ingestion.trader_api", "edge_agent")
_TraderClient = _trader_mod.TraderAPIClient

# Per-sport on-demand refresh rate limiter (unix timestamp of last trigger)
_ONDEMAND_REFRESH_COOLDOWN: dict[str, float] = {}

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
            title   = r.get("title", "").strip()
            content = r.get("content", "").strip()[:200]
            url     = r.get("url", "")
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
            title   = r.get("title", "").strip()
            snippet = r.get("snippet", "").strip()[:200]
            url     = r.get("link", "")
            lines.append(f"• {title}: {snippet}  [{url}]")
        lines.append("[End web search]")
        return "\n".join(lines)
    except Exception as exc:
        log.debug("Serper search failed: %s", exc)
        return ""


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)   # silence getUpdates poll noise
log = logging.getLogger("edge_bot")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BOT_TOKEN          = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID            = os.environ.get("TELEGRAM_CHAT_ID", "")
OWNER_ID           = os.environ.get("TELEGRAM_OWNER_ID", "")   # your personal user ID (from @userinfobot)
SCAN_INTERVAL_MIN    = int(os.environ.get("SCAN_INTERVAL_MINUTES", "180"))  # default 3 hours
INJURY_REFRESH_MIN   = int(os.environ.get("INJURY_REFRESH_MINUTES", "240"))  # default 4 hours
BANKROLL_USD         = float(os.environ.get("BANKROLL_USD", "10000"))

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

# Tracks already-alerted market keys to avoid duplicate alerts per scan cycle
_alerted_keys: set[str] = set()

# Approved signal types — only markets matching these signals will trigger alerts.
# Empty set means "alert on all" (bootstrapping mode until user approves something).
_approved_signals: set[str] = _load_approved_signals()

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
        _scanner = EdgeScanner(adapters=[
            KalshiAdapter(),
            PolymarketAdapter(),
        ])
    return _scanner


# ---------------------------------------------------------------------------
# Alert formatting
# ---------------------------------------------------------------------------

_SIGNAL_EMOJI = {
    "INJURY_MOMENTUM_REVERSAL": "🔥",
    "PRE_GAME_INJURY_LAG":      "🏥",
    "NEWS_LAG":                 "📰",
    "FAVORITE_LONGSHOT_BIAS":   "📈",
    "NONE":                     "📊",
}

_QUAL_EMOJI = {
    "qualified": "🟢",
    "watchlist": "🟡",
    "rejected":  "🔴",
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
    return (
        f"{'🔥' if g.triggered else '👁'} <b>[{_e(g.phase.value)}]</b> "
        f"<code>{_e(g.question[:60])}</code>\n"
        f"  Pre-game: {g.reference_prob:.1%} → Now: {g.last_market_prob:.1%}  ({_e(flag)})"
    )


# ---------------------------------------------------------------------------
# Scan helpers
# ---------------------------------------------------------------------------

async def _run_scan(bot, notify: bool = True) -> str:
    global _last_status, _alerted_keys

    svc = _get_service()
    scanner = _get_scanner()
    loop = asyncio.get_event_loop()

    try:
        # ── Run all blocking I/O in a thread pool so the bot stays responsive ──
        # scanner.collect() hits Kalshi/Polymarket HTTP APIs (can take 10-30s)
        inputs = await loop.run_in_executor(None, scanner.collect)
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

            _alerted_keys.add(key)
            new_alerts += 1

            if notify and bot:
                slot = _store_rec(rec)
                # callback_data max = 64 bytes — use short slot key, not raw market_id
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Approve", callback_data=f"a:{slot}"),
                    InlineKeyboardButton("❌ Skip",    callback_data=f"s:{slot}"),
                    InlineKeyboardButton("ℹ️ Details", callback_data=f"d:{slot}"),
                ]])
                await bot.send_message(
                    chat_id=CHAT_ID,
                    text=_fmt_alert(rec),
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )

        # Check for GameTracker triggers and notify
        triggered = svc.engine.game_tracker.triggered_games()
        for game in triggered:
            tkey = f"trigger:{game.venue.value}:{game.market_id}"
            if tkey not in _alerted_keys:
                _alerted_keys.add(tkey)
                if notify and bot:
                    await bot.send_message(
                        chat_id=CHAT_ID,
                        text=(
                            f"🔥 <b>GAME TRACKER TRIGGER FIRED</b>\n"
                            f"<i>{_e(game.question[:80])}</i>\n\n"
                            f"Phase: <code>{_e(game.phase.value)}</code>\n"
                            f"Pre-game: {game.reference_prob:.1%} → Now: {game.trigger_prob:.1%}\n"
                            f"Drop: {game.reference_prob - game.trigger_prob:.1%}\n\n"
                            f"Signal: <code>INJURY_MOMENTUM_REVERSAL</code>"
                        ),
                        parse_mode=ParseMode.HTML,
                    )

        tracker_text = svc.game_tracker_summary()

        # Build injury alert block — independent of qualification pipeline
        # (calls BallDontLie HTTP + injury cache, so run off the event loop)
        injury_alert_block = await loop.run_in_executor(None, _build_tonight_injury_alerts)

        # Fetch sportsbook lines for any sport that has alerts (1 search per sport)
        # Each call hits Tavily/Serper HTTP — run in executor to avoid blocking
        book_lines_block = ""
        if injury_alert_block:
            for _sp in ("nba", "nfl", "nhl"):
                if _sp in injury_alert_block.lower():
                    _lines = await loop.run_in_executor(None, _fetch_sportsbook_lines, _sp)
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
                    _sl.log_signal(
                        scan_run_id=_run_id,
                        market_id=_rec.market_id,
                        venue=_rec.venue.value,
                        signal_type=_rec.metadata.get("signal"),
                        ev_net=_rec.ev_net,
                        confidence=_rec.confidence,
                        action=_rec.action,
                        market_prob=_rec.market_prob,
                    )
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
        "👋 <b>EDGE Agent online.</b>\n\n"
        "<b>Commands:</b>\n"
        "/scan — run market scan now\n"
        "/injuries — injury cache summary\n"
        "/injuries nba — full NBA injury list\n"
        "/injuries nfl — full NFL injury list\n"
        "/injuries nhl — full NHL injury list\n"
        "/injuries nhl oilers — filter by team\n"
        "/tracking — injury game tracking list\n"
        "/top — top 3 opportunities\n"
        "/traders — top 20 smart money traders (auto-cached, instant)\n"
        "/traders sports — filter by category (sports/politics/crypto)\n"
        "/wallet 0x… — deep vet any Polymarket wallet\n"
        "/performance — scan stats: signals found, avg EV, best pick\n"
        "/performance 7 — last 7 days\n"
        "/status — last scan summary\n"
        "/approvals — manage alert signal filter\n"
        "/help — this message\n\n"
        f"{filter_note}\n"
        f"⏱ Market scan every {SCAN_INTERVAL_MIN // 60}h | "
        "Injury refresh: 9am, 1:30pm, 4:30pm PT.\n\n"
        "💬 Send any message to chat with EDGE about markets.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, ctx)


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
        await update.message.reply_text("👁 No games currently in the injury tracking list.")
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
    args     = (update.message.text or "").split()
    category = args[1].upper() if len(args) > 1 else "OVERALL"
    valid_cats = {"OVERALL", "SPORTS", "POLITICS", "CRYPTO", "CULTURE",
                  "ECONOMICS", "FINANCE", "TECH"}
    if category not in valid_cats:
        category = "OVERALL"

    from edge_agent.memory.trader_cache import TraderCache
    _TraderScore = _trader_mod.TraderScore  # already imported via importlib at top

    # ── Cache-first: pre-warmed by daily job, instant response ──────────────
    cache     = TraderCache()
    cache_rows = cache.get_top(20)

    if cache_rows:
        # Convert SQLite dicts → TraderScore objects for uniform display
        _fields = _TraderScore.__dataclass_fields__
        scores  = [_TraderScore(**{k: v for k, v in r.items() if k in _fields})
                   for r in cache_rows]
        st      = cache.stats()
        source_note = (
            f"<i>Smart money cache — {st['count']} traders | "
            f"Updated: {st['last_fetch']}</i>"
        )
    else:
        # Cache empty — score live (happens on first boot before warmup job runs)
        await update.message.reply_text(
            f"⏳ Cache empty — scoring top Polymarket traders ({category}) live (~30s)…"
        )
        try:
            client = _TraderClient()
            scores = await asyncio.get_event_loop().run_in_executor(
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
        name   = _e(ts.display_name or ts.wallet_address[:10] + "…")
        badge  = " ✅" if ts.verified else ""
        score  = int(ts.final_score * 100)
        wr7    = f"{ts.win_rate_7d:.0%}"
        pnl30  = f"+${ts.pnl_30d:,.0f}" if ts.pnl_30d >= 0 else f"-${abs(ts.pnl_30d):,.0f}"
        pnl7   = f"+${ts.pnl_7d:,.0f}"  if ts.pnl_7d  >= 0 else f"-${abs(ts.pnl_7d):,.0f}"
        streak = f"🔥{ts.current_streak}W" if ts.current_streak >= 2 else f"{ts.current_streak}W"
        risk   = (f" ⚠️{ts.unsettled_count} open" if ts.unsettled_count else "")

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
            f"   7d {wr7} {pnl7}  ·  30d {pnl30}  ·  {streak}{risk}"
        )

    lines.append(f"\n{source_note}")
    lines.append("<i>Use /wallet 0x… to deep-dive any trader.</i>")
    await _send_chunked(update.message.reply_text, "\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_wallet(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /wallet {address}
    Full vet of a specific Polymarket wallet address.
    """
    parts   = (update.message.text or "").split()
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
        ts = await asyncio.get_event_loop().run_in_executor(
            None, lambda: client.score_trader(address)
        )
    except Exception as exc:
        await update.message.reply_text(f"❌ Wallet vet failed: {exc}")
        return

    score  = int(ts.final_score * 100)
    ab     = int(ts.anti_bot_score    * 100)
    pf     = int(ts.performance_score * 100)
    rl     = int(ts.reliability_score * 100)

    if ts.bot_flag:
        verdict = "⚠️ LIKELY BOT"
    elif score >= 75:
        verdict = "✅ STRONG TRADER"
    elif score >= 55:
        verdict = "🟡 LEGIT TRADER"
    else:
        verdict = "🔴 WEAK RECORD"

    rl_tag  = " ⚠️" if rl < 70 else ""
    timing  = int(ts.timing_score      * 100)
    consist = int(ts.consistency_score * 100)
    fade    = int(ts.fade_score        * 100)
    sizing  = int(ts.sizing_discipline * 100)

    timing_label  = "Early/contrarian" if timing  >= 60 else ("Late to market"  if timing  < 35 else "Average timing")
    consist_label = "Steady earner"    if consist >= 60 else ("One-hit wonder?" if consist < 35 else "Moderate variance")
    fade_label    = "Contrarian"       if fade    >= 60 else ("Follows crowd"   if fade    < 35 else "Mixed style")
    sizing_label  = "Sizes up on edge" if sizing  >= 60 else ("Flat/undisciplined" if sizing < 35 else "Moderate")

    lines  = [
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
        adj_note = (f" (adj: {_fmt_pnl(ts.pnl_alltime_adj)})"
                    if ts.hidden_loss_exposure > 0 else "")
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

    await update.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML
    )


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
        data = await asyncio.get_event_loop().run_in_executor(
            None, lambda: ScanLog().get_summary(days=days)
        )
    except Exception as exc:
        await update.message.reply_text(f"❌ Performance data unavailable: {exc}")
        return

    scans  = data["scans"]
    qual   = data["total_qualified"]
    watch  = data["total_watchlist"]
    alerts = data["total_alerts"]
    avg_q  = data["avg_qual_per_scan"]

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
            ev_pct = f"{sig['avg_ev']*100:+.1f}%"
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
            f"EV: <b>{best['ev_net']*100:+.1f}%</b> | "
            f"Conf: {best['confidence']:.0%}\n"
            f"  Found: {best['ts_str']}"
        )

    # Smart money cache stats
    try:
        from edge_agent.memory.trader_cache import TraderCache
        st = TraderCache().stats()
        if st["count"]:
            lines.append(
                f"\n📈 <b>Smart Money Cache:</b> "
                f"{st['count']} traders | "
                f"Avg score: {st['avg_score']:.0f} | "
                f"Updated: {st['last_fetch']}"
            )
    except Exception:
        pass

    await _send_chunked(update.message.reply_text, "\n".join(lines), parse_mode=ParseMode.HTML)


async def trader_refresh_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily 8am PT — warm the trader cache with full top-100 leaderboard scores."""
    log.info("Trader refresh triggered.")
    try:
        loop   = asyncio.get_event_loop()
        client = _TraderClient()
        scores = await loop.run_in_executor(
            None, lambda: client.get_hot_traders(limit=100, category="OVERALL")
        )
        log.info("Trader refresh complete — %d traders scored.", len(scores))
    except Exception as exc:
        log.warning("Trader refresh failed: %s", exc)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_chunked(update.message.reply_text, _last_status)


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
                sig_added = f"\n📌 Signal type <code>{_e(sig)}</code> added to approved list."

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
        await query.message.reply_text(f"❌ Skipped: <code>{_e(label)}</code>", parse_mode=ParseMode.HTML)

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
        loop = asyncio.get_event_loop()
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
        _injury_detect = importlib.import_module(".dat-ingestion.injury_api", "edge_agent")
        detect_sport   = _injury_detect.detect_sport
        _star_keys     = set(_injury_detect._STAR_MULTIPLIERS.keys())

        # ── Detect sport ──────────────────────────────────────────────────────
        # Quick bail-out: if none of the sport-indicator words appear, skip.
        _SPORT_TRIGGERS = {
            "nba": {"nba", "basketball", "lakers", "celtics", "warriors", "bucks",
                    "heat", "nets", "knicks", "nuggets", "suns", "sixers", "raptors",
                    "mavericks", "mavs", "spurs", "thunder", "grizzlies", "pelicans"},
            "nfl": {"nfl", "football", "chiefs", "eagles", "cowboys", "ravens",
                    "bills", "bengals", "dolphins", "steelers", "49ers", "rams",
                    "seahawks", "patriots", "packers", "bears", "giants", "saints",
                    "buccaneers", "chargers", "raiders", "broncos", "texans"},
            "nhl": {"nhl", "hockey", "oilers", "bruins", "rangers", "leafs",
                    "canadiens", "penguins", "capitals", "lightning", "golden knights",
                    "kraken", "avalanche", "flames", "canucks", "senators", "sabres"},
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
            matched_sport = detect_sport(q)   # let keyword scorer decide

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
            relevant = [r for r in all_records if player_mentioned in r.get("player_name", "").lower()]
        else:
            # Try substring team match
            relevant = [r for r in all_records if any(w in q for w in r.get("team", "").lower().split())]

        # Fallback: show the most-severe players (top 10) for the detected sport
        if not relevant:
            relevant = all_records[:10]

        # ── Format — split starters vs role players ───────────────────────────
        _SEV_TAG = {
            "Out": "OUT", "Injured Reserve": "OUT(IR)", "Suspension": "SUSP",
            "Doubtful": "DOUBTFUL", "Questionable": "QUEST", "Day-To-Day": "DTD",
        }
        src_note = {"nba_official": "(official)", "+sleeper✓": "(confirmed)", "⚠️": "(⚠️ conflicting)"}

        def _fmt_row(r: dict) -> str:
            status   = r.get("status", "")
            tag      = _SEV_TAG.get(status, status)
            player   = r.get("player_name", "")
            team     = r.get("team", "")
            pos      = r.get("position", "")
            inj_type = r.get("injury_type", "")
            src      = r.get("source_api", "espn")
            src_tag  = next((v for k, v in src_note.items() if k in src), "")
            detail   = f" [{inj_type}]" if inj_type else ""
            pos_s    = f" ({pos})" if pos else ""
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
            for r in role_players[:5]:   # condensed — less critical
                lines.append(_fmt_row(r))

        import time as _t
        newest_ts = max((r.get("fetched_at", 0) for r in relevant), default=0)
        if newest_ts:
            age_min = int((_t.time() - newest_ts) / 60)
            age_str = f"{age_min}m ago" if age_min < 60 else f"{age_min // 60}h {age_min % 60}m ago"
        else:
            age_str = "unknown"
        sources = "ESPN"
        if matched_sport == "nba":
            sources += " + NBA official PDF"
        if matched_sport in ("nba", "nfl"):
            sources += " + Sleeper cross-ref"
        lines.append(f"[Source: {sources}. Last updated {age_str}. "
                     f"Use /injuries {matched_sport} to force-refresh.]")
        return "\n".join(lines)

    except Exception as exc:
        log.debug("Could not build injury context for chat: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# Free-form AI chat
# ---------------------------------------------------------------------------

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user_msg = update.message.text

    if not user_msg:
        return

    await update.message.chat.send_action("typing")

    # ── Correction detection ──────────────────────────────────────────────────
    # If the user signals the previous answer was wrong, we expand the search
    # query and inject a CORRECTION MODE instruction into the system prompt so
    # the AI knows NOT to repeat its prior response.
    _CORRECTION_TRIGGERS = {
        "wrong", "try again", "retry", "that's not", "thats not",
        "incorrect", "not right", "different answer", "try harder",
        "still wrong", "no that", "you said", "that was wrong",
        "redo", "search again", "look again", "check again", "that's wrong",
        "thats wrong", "bad answer", "wrong answer",
    }
    _is_correction = any(t in user_msg.lower() for t in _CORRECTION_TRIGGERS)

    # 1. Knowledge base context
    kb_context = _kb.get_context_for_question(user_msg)

    # 2. Session memory context
    session_context = _mem.get_session_context(max_exchanges=4)

    # 3. Live market context (on-demand, only for market questions)
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
    for keywords, series in _SERIES_MAP:
        if any(kw in q for kw in keywords):
            try:
                markets = _kalshi_api.get_markets(limit=3, series_ticker=series, min_volume=1)
                if markets:
                    lines = [f"\nLive {series} markets:"]
                    for m in markets:
                        prob = _kalshi_api.parse_market_prob(m)
                        vol = _kalshi_api.parse_volume(m)
                        lines.append(f"- {m.get('title', m.get('ticker'))}: {prob:.0%} yes | ${vol:,.0f} vol")
                    market_context = "\n".join(lines)
            except Exception:
                pass
            break

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
        "nba": {"nba", "basketball", "lakers", "celtics", "warriors", "bucks", "heat",
                "nets", "knicks", "nuggets", "suns", "sixers", "raptors", "mavericks",
                "mavs", "spurs", "thunder", "grizzlies", "pelicans", "kings", "bulls",
                "rockets", "jazz", "clippers", "pistons", "hornets", "magic", "hawks",
                "pacers", "cavaliers", "wizards", "timberwolves", "trail blazers"},
        "nfl": {"nfl", "football", "chiefs", "eagles", "cowboys", "ravens", "bills",
                "bengals", "dolphins", "steelers", "49ers", "rams", "seahawks",
                "patriots", "packers", "bears", "giants", "saints", "buccaneers",
                "chargers", "raiders", "broncos", "texans", "colts", "titans",
                "jaguars", "browns", "falcons", "panthers", "cardinals", "vikings"},
        "nhl": {"nhl", "hockey", "oilers", "bruins", "rangers", "leafs", "canadiens",
                "penguins", "capitals", "lightning", "golden knights", "kraken",
                "avalanche", "flames", "canucks", "senators", "sabres", "coyotes",
                "sharks", "ducks", "kings", "blues", "predators", "wild", "jets",
                "red wings", "islanders", "devils", "flyers", "hurricanes"},
    }
    _chat_sport = None
    for _sp, _triggers in _SPORT_DETECT.items():
        if any(_t in q for _t in _triggers):
            _chat_sport = _sp
            break
    if _chat_sport:
        await _maybe_refresh_injury_cache(_chat_sport)

    injury_context = _build_injury_context(q)

    # 6. Real-time web search — fires when a sport is detected OR when the user
    #    is correcting a previous answer (correction forces a fresh search even
    #    without a sport keyword so the AI has new data to work from).
    #    Tries Tavily first (AI-native, 1,000/mo free), then falls back to
    #    Serper (Google results, 2,500/mo free) if Tavily quota is exhausted.
    search_context = ""
    if _chat_sport or _is_correction:
        if _is_correction and _chat_sport:
            # Expand query to force fresher / different results on correction
            _query = f"{user_msg} {_chat_sport.upper()} latest confirmed update today"
        elif _is_correction:
            # No sport detected but user is correcting — search exactly what they said
            _query = f"{user_msg} latest news today confirmed"
        else:
            _query = f"{user_msg} {_chat_sport.upper()} injury report today"
        loop = asyncio.get_event_loop()
        search_context = await loop.run_in_executor(None, _tavily_search, _query)
        if not search_context:   # Tavily failed/quota gone → try Serper
            search_context = await loop.run_in_executor(None, _serper_search, _query)

    # 7. Win-probability impact context — injected when a sport is detected.
    #    Prepended to search_context so the AI reasons with actual shift math
    #    (e.g. "LeBron Out → -12.3% win-prob shift, 10.5 pts/gm impact") rather
    #    than generic statements about a player being injured.
    if _chat_sport and _chat_sport != "unknown":
        wp_context = _build_win_prob_context(_chat_sport)
        if wp_context:
            search_context = wp_context + ("\n\n" + search_context if search_context else "")

    # ── Correction mode instruction — prepended to system prompt ─────────────
    correction_instruction = (
        "CORRECTION MODE ACTIVE: The user indicated your previous response was wrong. "
        "Do NOT repeat or rephrase your previous answer. "
        "Search the [Live web search results] block below for updated information and use that. "
        "If the search results contradict what you said before, use the search results. "
        "If you still cannot find clear data, say exactly what you found and what is uncertain — "
        "do not guess or hallucinate.\n\n"
        if _is_correction else ""
    )

    system_prompt = (
        correction_instruction
        + "You are Edge, an expert prediction market analyst on Telegram. "
        "Be concise — Telegram users want short, direct answers. "
        "Reference live market data and knowledge base context when provided. "
        "Use session context to remember what was discussed earlier. "
        "If asked about account setup or platform UI, give step-by-step guidance. "
        "Return plain text (no JSON). Keep replies under 300 words.\n\n"
        "INJURY DATA RULES — apply ONLY when the user's current message explicitly "
        "asks about injuries, player health, roster status, or a specific game matchup:\n"
        "• If injury data IS in [Live injury data] or [Live web search results]: cite it.\n"
        "• If the player/team is NOT in those blocks: say 'I don't have current data "
        "for [name] — use /injuries nba (or nfl/nhl) to refresh.'\n"
        "• NEVER invent or recall injury statuses from training memory.\n"
        "• For ALL other questions (scan results, market edges, strategy, commands, "
        "general chat): answer normally — do NOT mention injuries or data blocks "
        "unless the user directly asked about them.\n\n"
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
        _mem.add_exchange(
            "[USER CORRECTION]",
            "[Bot acknowledged correction — performed expanded search]",
        )

    prompt = user_msg + kb_context + session_context + market_context + scan_context + injury_context + search_context
    reply = get_chat_response(prompt, task_type="creative", system_prompt=system_prompt) or "Sorry, I couldn't generate a response right now."

    # Save to session memory
    if reply:
        _mem.add_exchange(user_msg, reply)

    # Telegram max message length is 4096 chars
    if len(reply) > 4000:
        reply = reply[:4000] + "\n\n(truncated)"

    await update.message.reply_text(reply)


# ---------------------------------------------------------------------------
# /injuries command — enhanced with player list and team filtering
# ---------------------------------------------------------------------------

_SEVERITY_EMOJI = {
    "Out":             "🔴",
    "Injured Reserve": "🔴",   # NHL IR — confirmed miss, treated same as Out
    "Suspension":      "🚫",
    "Doubtful":        "🟠",
    "Questionable":    "🟡",
    "Day-To-Day":      "⚪",
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
        tonight_nba = _get_tonight_nba_games()   # frozenset of lowercase team tokens

        for sport in ("nba", "nfl", "nhl"):
            records = db.get_all(sport)
            if not records:
                continue
            key_pos = _KEY_POSITIONS.get(sport, set())
            alerts = [
                r for r in records
                if r.get("status", "").lower() in _ALERT_STATUSES
                and (not r.get("position") or r.get("position", "") in key_pos)
            ]
            if not alerts:
                continue

            # Header — tag NBA with "(Tonight's Games)" when schedule is available
            header_suffix = " (Tonight & Tomorrow)" if sport == "nba" and tonight_nba else ""
            lines.append(f"\n🏥 <b>{sport.upper()} Starter Alerts{header_suffix}:</b>")

            shown = 0
            for r in alerts:
                if shown >= 10:   # cap at 10 per sport to keep message compact
                    break
                name   = r.get("player_name", "Unknown")
                team   = r.get("team", "")
                status = r.get("status", "")
                src    = r.get("source_api", "")
                pos    = r.get("position", "")
                emoji  = _SEVERITY_EMOJI.get(status, "⚪")

                # NBA: filter to teams playing tonight when schedule data is available
                if sport == "nba" and tonight_nba:
                    team_tokens = set(team.lower().split())
                    if not team_tokens.intersection(tonight_nba):
                        continue   # this team isn't playing tonight — skip

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
                    unit      = "goals/gm" if sport == "nhl" else "pts/gm"
                    shift_str = f" → <b>{shift:+.1%}</b> win prob ({eff_impact:.1f} {unit})"
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
        result = result.replace("\n[Live web search results]\n", "").replace("\n[End web search]", "").strip()
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
    if _bdl_game_cache["teams"] is not None and now - _bdl_game_cache["fetched_at"] < 1800:
        return _bdl_game_cache["teams"]

    today    = datetime.now(timezone.utc).strftime("%Y-%m-%d")
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
        _bdl_game_cache["teams"]      = result
        _bdl_game_cache["fetched_at"] = now
        log.info("[BALLDONTLIE] Tonight+Tomorrow NBA team tokens: %s", sorted(result))
        return result
    except Exception as exc:
        log.warning("[BALLDONTLIE] Game fetch failed: %s", exc)
        _bdl_game_cache["teams"]      = frozenset()
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
        db      = InjuryCache()
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
      /injuries nba lakers — NBA players for the Lakers only
      /injuries nfl chiefs — NFL players for the Chiefs only
      /injuries nhl oilers — NHL players for the Oilers only
    """
    args = ctx.args or []
    sport_filter = args[0].lower() if args else None
    team_filter  = " ".join(args[1:]).lower() if len(args) > 1 else None

    try:
        from edge_agent.memory.injury_cache import InjuryCache
        cache = InjuryCache()

        # ── No sport arg: show summary ────────────────────────────────────────
        if not sport_filter or sport_filter not in ("nba", "nfl", "nhl"):
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
        starters_all   = [r for r in records if r.get("is_starter")]
        role_all       = [r for r in records if not r.get("is_starter")]
        header += f" ({len(starters_all)} starters · {len(role_all)} role)"

        def _player_line(r: dict) -> str:
            status    = r.get("status", "")
            sem       = _SEVERITY_EMOJI.get(status, "⚪")
            player    = r.get("player_name", "")
            pos       = r.get("position", "")
            inj_type  = r.get("injury_type", "")
            src       = r.get("source_api", "espn")
            pos_str    = f" ({_e(pos)})" if pos else ""
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
    for sport in ("nba", "nfl", "nhl"):
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
        pending = cache.get_pending_change_alerts()

        for alert in pending:
            player   = alert.get("player_name", "")
            team     = alert.get("team", "")
            pos      = alert.get("position", "")
            old_s    = alert.get("old_status", "")
            new_s    = alert.get("new_status", "")
            sport    = alert.get("sport", "").upper()

            old_em = _SEVERITY_EMOJI.get(old_s, "⚪")
            new_em = _SEVERITY_EMOJI.get(new_s, "🔴")
            pos_str = f" ({_e(pos)})" if pos else ""
            sport_emoji = "🏀" if sport == "NBA" else ("🏒" if sport == "NHL" else "🏈")

            msg = (
                f"🚨 <b>INJURY STATUS WORSENED</b>\n\n"
                f"{sport_emoji} <b>{_e(player)}</b>{pos_str}\n"
                f"<i>{_e(team)}</i> [{sport}]\n\n"
                f"{old_em} {_e(old_s)} → {new_em} <b>{_e(new_s)}</b>\n\n"
                f"<i>This may affect win-probability markets. "
                f"Run /scan for updated signals.</i>"
            )

            await ctx.bot.send_message(
                chat_id=CHAT_ID,
                text=msg,
                parse_mode=ParseMode.HTML,
            )
            log.info(
                "Proactive injury alert sent: %s %s → %s",
                player, old_s, new_s,
            )

    except Exception as exc:
        log.warning("Could not dispatch proactive injury alerts: %s", exc)


# ---------------------------------------------------------------------------
# Background scan job
# ---------------------------------------------------------------------------

async def scan_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.info("Background scan triggered.")
    result = await _run_scan(ctx.bot, notify=True)
    if "Scan error" in result:
        await ctx.bot.send_message(chat_id=CHAT_ID, text=f"⚠️ Scan error:\n{result}")
    else:
        log.info("Scan complete: %s", result.split("\n")[0])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

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

    log.info(
        "Starting EDGE Telegram bot (scan every %d min, injury refresh every %d min)...",
        SCAN_INTERVAL_MIN,
        INJURY_REFRESH_MIN,
    )

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # ---------------------------------------------------------------------------
    # Access control — two-layer whitelist:
    #   Layer 1: filters.Chat — bot only processes updates from TELEGRAM_CHAT_ID.
    #            Silently ignores every other group or DM the bot is added to.
    #   Layer 2: filters.User — within that chat, only TELEGRAM_OWNER_ID can
    #            trigger commands or AI chat. Other group members are ignored.
    #
    # Result: natural conversation feel (no @mention needed), bot responds only
    # to you, and is completely deaf to every other user in the group.
    #
    # Get your user ID: message @userinfobot on Telegram, then set in .env:
    #   TELEGRAM_OWNER_ID=<your numeric id>
    # ---------------------------------------------------------------------------
    try:
        _chat_filter = filters.Chat(int(CHAT_ID))
        log.info("Chat filter active: only responding to chat_id=%s", CHAT_ID)
    except (ValueError, TypeError):
        _chat_filter = filters.ALL
        log.warning("CHAT_ID=%r is not a valid integer — no chat filter applied", CHAT_ID)

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
        log.warning("OWNER_ID=%r is not a valid integer — no user filter applied", OWNER_ID)

    # Combined filter: must be from the authorized chat AND the authorized user
    _auth_filter = _chat_filter & _user_filter

    # Command handlers — only fire in the authorized chat
    app.add_handler(CommandHandler("start",     cmd_start,     filters=_auth_filter))
    app.add_handler(CommandHandler("help",      cmd_help,      filters=_auth_filter))
    app.add_handler(CommandHandler("scan",      cmd_scan,      filters=_auth_filter))
    app.add_handler(CommandHandler("injuries",  cmd_injuries,  filters=_auth_filter))
    app.add_handler(CommandHandler("injurys",   cmd_injuries,  filters=_auth_filter))  # typo alias
    app.add_handler(CommandHandler("tracking",  cmd_tracking,  filters=_auth_filter))
    app.add_handler(CommandHandler("top",       cmd_top,       filters=_auth_filter))
    app.add_handler(CommandHandler("traders",   cmd_traders,   filters=_auth_filter))
    app.add_handler(CommandHandler("wallet",      cmd_wallet,      filters=_auth_filter))
    app.add_handler(CommandHandler("performance", cmd_performance, filters=_auth_filter))
    app.add_handler(CommandHandler("status",      cmd_status,      filters=_auth_filter))
    app.add_handler(CommandHandler("approvals",   cmd_approvals,   filters=_auth_filter))

    # Inline keyboard (callback queries are always scoped to the chat they came from)
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Free-form AI chat — only in the authorized chat, must come last
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & _auth_filter,
        handle_message,
    ))

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
        dt_time(9,  0,  tzinfo=_PACIFIC),   # morning
        dt_time(13, 30, tzinfo=_PACIFIC),   # mid-day / NBA PDF window
        dt_time(16, 30, tzinfo=_PACIFIC),   # pre-game final
    ):
        app.job_queue.run_daily(injury_refresh_job, time=_pull_time)

    # Startup warmup — populate cache 60s after boot regardless of time of day
    app.job_queue.run_once(injury_refresh_job, when=60)

    # Trader leaderboard — refresh daily at 8am PT, warm cache 2 min after boot
    app.job_queue.run_daily(trader_refresh_job, time=dt_time(8, 0, tzinfo=_PACIFIC))
    app.job_queue.run_once(trader_refresh_job, when=120)

    # Background market scan loop — reads from injury cache, no live injury API calls
    app.job_queue.run_repeating(
        scan_job,
        interval=SCAN_INTERVAL_MIN * 60,
        first=90,  # first scan after injury cache is warm
    )

    log.info("Bot running. Press Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
