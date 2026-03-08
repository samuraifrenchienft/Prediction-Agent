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

# Per-sport on-demand refresh rate limiter (unix timestamp of last trigger)
_ONDEMAND_REFRESH_COOLDOWN: dict[str, float] = {}

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
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

    try:
        inputs = scanner.collect()
        recs, summary = svc.run_scan(inputs, portfolio=_portfolio)

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
        _last_status = (
            f"Scan @ {datetime.now(timezone.utc).strftime('%H:%M UTC')}\n"
            f"Markets: {summary.total_markets} | "
            f"Qualified: {summary.qualified} | "
            f"Watchlist: {summary.watchlist} | "
            f"Rejected: {summary.rejected}\n"
            f"New alerts: {new_alerts}\n\n"
            f"{tracker_text}"
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
        await update.message.reply_text(
            f"✅ Scan complete.\n<pre>{_e(result)}</pre>",
            parse_mode=ParseMode.HTML,
        )


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
    svc = _get_service()
    top = svc.engine.top_opportunities(limit=3)
    if not top:
        await update.message.reply_text("No opportunities recorded yet. Run /scan first.")
        return
    for rec in top:
        await update.message.reply_text(
            _fmt_alert(rec),
            parse_mode=ParseMode.HTML,
        )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"<pre>{_e(_last_status)}</pre>",
        parse_mode=ParseMode.HTML,
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

        # ── Format ────────────────────────────────────────────────────────────
        _SEV_TAG = {
            "Out": "OUT", "Injured Reserve": "OUT(IR)", "Suspension": "SUSP",
            "Doubtful": "DOUBTFUL", "Questionable": "QUEST", "Day-To-Day": "DTD",
        }
        src_note = {"nba_official": "(official)", "+sleeper✓": "(confirmed)", "⚠️": "(⚠️ conflicting)"}

        lines = [f"\n[Live {matched_sport.upper()} injury data from verified cache]"]
        for r in relevant[:15]:   # hard cap to keep prompt size reasonable
            status   = r.get("status", "")
            tag      = _SEV_TAG.get(status, status)
            player   = r.get("player_name", "")
            team     = r.get("team", "")
            pos      = r.get("position", "")
            inj_type = r.get("injury_type", "")
            src      = r.get("source_api", "espn")

            src_tag = ""
            for k, v in src_note.items():
                if k in src:
                    src_tag = f" {v}"
                    break

            detail = f" [{inj_type}]" if inj_type else ""
            pos_s  = f" ({pos})" if pos else ""
            lines.append(f"  {tag}: {player}{pos_s} — {team}{detail}{src_tag}")

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

    system_prompt = (
        "You are Edge, an expert prediction market analyst on Telegram. "
        "Be concise — Telegram users want short, direct answers. "
        "Reference live market data and knowledge base context when provided. "
        "Use session context to remember what was discussed earlier. "
        "If asked about account setup or platform UI, give step-by-step guidance. "
        "Return plain text (no JSON). Keep replies under 300 words.\n\n"
        "CRITICAL — INJURY DATA RULES (hard rules, no exceptions):\n"
        "• You have NO internet access. You CANNOT search the web, fetch URLs, "
        "call APIs, or retrieve anything live.\n"
        "• NEVER claim to 'perform a search', 'check live data', 'look up', or "
        "'fetch' anything. You physically cannot do this.\n"
        "• ONLY report injury information that is explicitly listed in the "
        "[Live injury data] block provided below. Do not add, guess, or recall "
        "any player status from your training knowledge.\n"
        "• If a player or team is NOT in the [Live injury data] block, say exactly: "
        "'I don't have current data for [name] — use /injuries nba (or nfl/nhl) "
        "to pull a fresh report.'\n"
        "• If no [Live injury data] block is present, say: "
        "'My injury cache is empty right now — use /injuries nba to refresh.'\n\n"
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

    prompt = user_msg + kb_context + session_context + market_context + scan_context + injury_context
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

        # Build the player-list message
        sport_label = sport.upper()
        header = f"<b>🏥 {sport_label} Injuries</b>"
        if team_filter:
            header += f" — <i>{_e(team_filter.title())}</i>"
        header += f" ({len(records)} players)"

        lines = [header, ""]
        current_team = None
        shown = 0

        for r in records:
            if shown >= _INJURIES_MAX_PER_SPORT:
                lines.append(
                    f"\n<i>... and {len(records) - shown} more. "
                    f"Use /injuries {sport} [team] for filtered view.</i>"
                )
                break

            team = r.get("team", "")
            if team != current_team:
                if current_team is not None:
                    lines.append("")       # blank line between teams
                current_team = team
                lines.append(f"<b>{_e(team)}</b>")

            status    = r.get("status", "")
            sem       = _SEVERITY_EMOJI.get(status, "⚪")
            player    = r.get("player_name", "")
            pos       = r.get("position", "")
            inj_type  = r.get("injury_type", "")
            src       = r.get("source_api", "espn")

            pos_str    = f" ({_e(pos)})" if pos else ""
            detail_str = f" — <i>{_e(inj_type)}</i>" if inj_type else ""
            # Source badge: ✅ = multi-source confirmed | ⚠️ = conflicting sources | 📰 = news confirmed
            if "nba_official" in src or "+sleeper✓" in src:
                src_badge = " ✅"
            elif "⚠️" in src:
                src_badge = " ⚠️"
            elif "news✓" in src:
                src_badge = " 📰"
            else:
                src_badge = ""

            lines.append(
                f"  {sem} <b>{_e(player)}</b>{pos_str}: {_e(status)}{detail_str}{src_badge}"
            )
            shown += 1

        lines.append(
            "\n<i>✅ multi-source confirmed | ⚠️ conflicting sources (treat as uncertain) | 📰 news confirmed</i>"
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
    app.add_handler(CommandHandler("tracking",  cmd_tracking,  filters=_auth_filter))
    app.add_handler(CommandHandler("top",       cmd_top,       filters=_auth_filter))
    app.add_handler(CommandHandler("status",    cmd_status,    filters=_auth_filter))
    app.add_handler(CommandHandler("approvals", cmd_approvals, filters=_auth_filter))

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
