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

Commands in Telegram:
  /scan       — run a full market scan immediately
  /tracking   — show the injury game tracking list
  /top        — show top 3 opportunities from last scan
  /status     — show last scan summary
  /help       — command list

  Or just send any message to chat with EDGE about markets.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
from datetime import datetime, timezone

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

from edge_agent import (
    EdgeEngine,
    EdgeScanner,
    EdgeService,
    JupiterAdapter,
    KalshiAdapter,
    PolymarketAdapter,
    PortfolioState,
)
from edge_agent.ai_service import get_ai_chat_response
from edge_agent.game_tracker import TrackedGame
from edge_agent.models import Recommendation

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("edge_bot")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BOT_TOKEN          = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID            = os.environ.get("TELEGRAM_CHAT_ID", "")
SCAN_INTERVAL_MIN  = int(os.environ.get("SCAN_INTERVAL_MINUTES", "15"))
BANKROLL_USD       = float(os.environ.get("BANKROLL_USD", "10000"))

# ---------------------------------------------------------------------------
# Global state (shared across handlers)
# ---------------------------------------------------------------------------

_service: EdgeService | None = None
_scanner: EdgeScanner | None = None
_portfolio = PortfolioState(bankroll_usd=BANKROLL_USD)

# Tracks already-alerted market keys to avoid duplicate alerts per scan cycle
_alerted_keys: set[str] = set()

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
            JupiterAdapter(),
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
    "CROSS_MARKET_CORRELATION": "🔗",
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
    await update.message.reply_text(
        "👋 <b>EDGE Agent online.</b>\n\n"
        "<b>Commands:</b>\n"
        "/scan — run market scan now\n"
        "/tracking — injury game tracking list\n"
        "/top — top 3 opportunities\n"
        "/status — last scan summary\n"
        "/help — this message\n\n"
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
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            f"✅ <b>Approved:</b> <code>{_e(label)}</code>\n"
            f"<i>Proposal recorded. No live trade placed — this is a proposal-only system.</i>",
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
# Free-form AI chat
# ---------------------------------------------------------------------------

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user_msg = update.message.text

    if not user_msg:
        return

    await update.message.chat.send_action("typing")

    # Build market context from recent scan results
    svc = _get_service()
    top = svc.engine.top_opportunities(limit=5)
    context_lines = []
    if top:
        context_lines.append("Recent top opportunities:")
        for r in top:
            context_lines.append(
                f"- [{r.metadata.get('signal','?')}] {r.metadata.get('question', r.market_id)[:60]} "
                f"| market={r.market_prob:.0%} agent={r.agent_prob:.0%} ev={r.ev_net:+.1%}"
            )
    games = svc.engine.game_tracker.active_games()
    if games:
        context_lines.append(f"\nTracked injury games ({len(games)}):")
        for g in games:
            context_lines.append(
                f"- [{g.phase.value}] {g.question[:50]} | pre={g.reference_prob:.0%} now={g.last_market_prob:.0%}"
            )

    context = "\n".join(context_lines)

    # Maintain per-user conversation history (last 10 turns)
    history = _chat_history.get(user_id, [])
    reply = get_ai_chat_response(
        user_message=user_msg,
        context=context,
        conversation_history=history,
    )

    # Update history
    history.append({"role": "user", "content": user_msg})
    history.append({"role": "assistant", "content": reply})
    _chat_history[user_id] = history[-20:]  # keep last 10 turns (20 messages)

    # Telegram max message length is 4096 chars
    if len(reply) > 4000:
        reply = reply[:4000] + "\n\n_(truncated)_"

    await update.message.reply_text(reply)


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

    log.info("Starting EDGE Telegram bot (scan every %d min)...", SCAN_INTERVAL_MIN)

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Command handlers
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("scan",     cmd_scan))
    app.add_handler(CommandHandler("tracking", cmd_tracking))
    app.add_handler(CommandHandler("top",      cmd_top))
    app.add_handler(CommandHandler("status",   cmd_status))

    # Inline keyboard
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Free-form AI chat (must come last)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Background scan loop
    app.job_queue.run_repeating(
        scan_job,
        interval=SCAN_INTERVAL_MIN * 60,
        first=30,  # first scan 30 seconds after startup
    )

    log.info("Bot running. Press Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
