"""
EDGE Discord Bot
================
Serves paying users — each user gets their own private Discord channel
created automatically when the owner runs /adduser.

Setup:
  1. Go to https://discord.com/developers/applications
  2. New Application → Bot → copy token → DISCORD_BOT_TOKEN in .env
  3. Enable Privileged Intents: Server Members + Message Content
  4. Invite bot to your server with scopes: bot + applications.commands
     Permissions needed: Manage Channels, Send Messages, Read Message History,
                         Embed Links, Use Slash Commands, Add Reactions
  5. Add to .env:
       DISCORD_BOT_TOKEN=<token>
       DISCORD_GUILD_ID=<your server ID (right-click server → Copy ID)>
       DISCORD_CATEGORY_ID=<category channel ID where user channels go>
       DISCORD_OWNER_ID=<your Discord user ID>
  6. python run_discord_bot.py

User workflow:
  - Owner: /adduser @member  → bot creates #edge-username, pings them
  - User: types in their channel, runs slash commands, gets all alerts
  - Owner: /removeuser @member  → bot removes their channel + access
"""

from __future__ import annotations

import asyncio
import html
import importlib
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BOT_TOKEN    = os.environ.get("DISCORD_BOT_TOKEN", "")
GUILD_ID     = int(os.environ.get("DISCORD_GUILD_ID", "0") or "0")
CATEGORY_ID  = int(os.environ.get("DISCORD_CATEGORY_ID", "0") or "0")
OWNER_ID     = int(os.environ.get("DISCORD_OWNER_ID", "0") or "0")

SCAN_INTERVAL_MIN   = int(os.environ.get("SCAN_INTERVAL_MINUTES", "180"))
INJURY_REFRESH_MIN  = int(os.environ.get("INJURY_REFRESH_MINUTES", "240"))
BANKROLL_USD        = float(os.environ.get("BANKROLL_USD", "10000"))

# ---------------------------------------------------------------------------
# Engine imports (same as Telegram bot)
# ---------------------------------------------------------------------------
from edge_agent import (
    EdgeService,
    KalshiAdapter,
    PolymarketAdapter,
    PortfolioState,
)
from edge_agent.ai_service import get_chat_response
from edge_agent.memory import KnowledgeBase, SessionMemory
from edge_agent.memory.outcome_tracker import OutcomeTracker as _OutcomeTracker
from edge_agent.memory.user_profile import UserProfileStore as _UserProfileStore
from edge_agent.memory.channel_registry import ChannelRegistry as _ChannelRegistry
from edge_agent.models import Recommendation
from edge_agent.memory.trader_cache import TraderCache as _TraderCache
from edge_agent.insider_alerts import InsiderAlertEngine as _InsiderAlertEngine
from edge_agent.memory.decision_log import DecisionLog as _DecisionLog
from edge_agent.ml.ml_store import MLStore as _MLStore
from edge_agent.ml.signal_scorer import SignalScorer as _SignalScorer
from edge_agent.ml.confidence_calibrator import ConfidenceCalibrator as _ConfidenceCalibrator
from edge_agent.ml.regime_detector import RegimeDetector as _RegimeDetector
from edge_agent.prompt_registry import get_registry as _get_prompt_registry
import time

try:
    _scanner_mod   = importlib.import_module("edge_agent.scanner")
    EdgeScanner    = _scanner_mod.EdgeScanner
except Exception:
    EdgeScanner    = None  # type: ignore

try:
    _standings_mod    = importlib.import_module("edge_agent.dat-ingestion.standings_api",)
except Exception:
    try:
        import importlib
        _standings_mod = importlib.import_module("edge_agent.dat_ingestion.standings_api")
    except Exception:
        _standings_mod = None

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
_service: EdgeService | None   = None
_scanner                       = None
_portfolio = PortfolioState(bankroll_usd=BANKROLL_USD)
_kb        = KnowledgeBase()
_ot        = _OutcomeTracker()
_profiles  = _UserProfileStore()
_registry  = _ChannelRegistry()   # reuse same SQLite registry as Telegram

_alerted_keys: set[str] = set()
_last_status: str        = "No scan yet"

# Shared singletons
_tc             = _TraderCache()
_decision_log   = _DecisionLog()
_ml_store       = _MLStore()
_signal_scorer  = _SignalScorer()
_calibrator     = _ConfidenceCalibrator(_ml_store)
_regime         = _RegimeDetector(_ml_store)
_prompt_registry = _get_prompt_registry()
_calibrator.load()
_signal_scorer.load()

# Smart money cache
_sm_positions_cache: dict = {}
_sm_cache_ts: float = 0.0
_SM_CACHE_TTL = 1800   # 30 min
_sm_prev_positions: dict = {}  # for new-position detection

# Insider engine
_insider_engine: _InsiderAlertEngine | None = None

def _get_insider_engine() -> _InsiderAlertEngine:
    global _insider_engine
    if _insider_engine is None:
        _insider_engine = _InsiderAlertEngine()
    return _insider_engine

# Approved signals set (mirrors Telegram)
_APPROVALS_FILE = Path("edge_agent/memory/data/approvals.json")

def _load_approved() -> set[str]:
    try:
        if _APPROVALS_FILE.exists():
            return set(json.loads(_APPROVALS_FILE.read_text()))
    except Exception:
        pass
    return set()

_approved_signals: set[str] = _load_approved()

import json

# ---------------------------------------------------------------------------
# Helpers — engine access
# ---------------------------------------------------------------------------

def _get_service() -> EdgeService:
    global _service
    if _service is None:
        _service = EdgeService()
    return _service


def _get_scanner():
    global _scanner
    if _scanner is None and EdgeScanner is not None:
        _scanner = EdgeScanner(adapters=[KalshiAdapter(), PolymarketAdapter()])
    return _scanner


def _fetch_all_open_markets_discord(limit: int = 200) -> list[dict]:
    """
    Fetch open markets from Kalshi for specialist scanners (weather/crypto/econ).
    Returns normalised list of dicts: {title, price, ticker, venue}.
    """
    markets: list[dict] = []
    try:
        _kalshi = importlib.import_module("edge_agent.dat-ingestion.kalshi_api")
        ka      = _kalshi.KalshiAPIClient()
        raw     = ka.get_markets(status="open", limit=limit)
        for m in (raw or []):
            markets.append({
                "title":  m.get("title", m.get("question", "")),
                "price":  float(m.get("yes_bid", m.get("last_price", 0.5)) or 0.5),
                "ticker": m.get("ticker", m.get("id", "")),
                "venue":  "kalshi",
            })
    except Exception as exc:
        log.debug("[Discord/SpecialistScan] Market fetch failed: %s", exc)
    return markets


# ---------------------------------------------------------------------------
# Discord embed formatters
# ---------------------------------------------------------------------------

_SIGNAL_COLOUR = {
    "MISPRICED_YES":           0x00C851,  # green
    "MISPRICED_NO":            0xFF4444,  # red
    "MOMENTUM":                0xFF8800,  # orange
    "SMART_MONEY":             0xAA00FF,  # purple
    "INJURY_MOMENTUM_REVERSAL":0xFF6600,  # deep orange
}
_DEFAULT_COLOUR = 0x2196F3  # blue

_SIGNAL_EMOJI = {
    "MISPRICED_YES":            "📈",
    "MISPRICED_NO":             "📉",
    "MOMENTUM":                 "🔄",
    "SMART_MONEY":              "🐋",
    "INJURY_MOMENTUM_REVERSAL": "🚑",
    "NONE":                     "📊",
}


def _embed_alert(rec: Recommendation) -> discord.Embed:
    signal   = rec.metadata.get("signal", "NONE")
    question = rec.metadata.get("question") or rec.market_id
    colour   = _SIGNAL_COLOUR.get(signal, _DEFAULT_COLOUR)
    em       = _SIGNAL_EMOJI.get(signal, "📊")

    embed = discord.Embed(
        title       = f"{em} {signal}  —  {rec.action}",
        description = question[:200],
        colour      = colour,
        timestamp   = datetime.now(timezone.utc),
    )
    embed.add_field(name="Market",      value=f"{rec.market_prob:.1%}", inline=True)
    embed.add_field(name="Agent",       value=f"{rec.agent_prob:.1%}",  inline=True)
    embed.add_field(name="Edge",        value=f"{rec.edge:+.1%}",       inline=True)
    embed.add_field(name="EV net",      value=f"{rec.ev_net:+.2%}",     inline=True)
    embed.add_field(name="Confidence",  value=f"{rec.confidence:.0%}",  inline=True)
    embed.add_field(name="Venue",       value=rec.venue.value,           inline=True)
    if rec.thesis:
        embed.add_field(name="Thesis", value=rec.thesis[0][:200], inline=False)
    embed.set_footer(text=f"Market ID: {rec.market_id[:60]}")
    return embed


def _embed_injury(
    player: str,
    team: str,
    sport: str,
    old_s: str,
    new_s: str,
    direction: str,
    position: str = "",
) -> discord.Embed:
    _SEVERITY_COLOUR = {
        "Out":          0xFF4444,
        "Doubtful":     0xFF8800,
        "Questionable": 0xFFBB00,
        "Day-To-Day":   0xFFDD00,
        "Active":       0x00C851,
    }
    if direction == "worsening":
        colour = _SEVERITY_COLOUR.get(new_s, 0xFF4444)
        title  = "🚨 INJURY STATUS WORSENED"
    else:
        colour = 0x00C851 if new_s == "Active" else 0xFFBB00
        title  = "🔓 PLAYER CLEARED" if new_s == "Active" else "📈 INJURY IMPROVING"

    pos_str = f" ({position})" if position else ""
    embed   = discord.Embed(
        title       = title,
        description = f"**{player}**{pos_str}  —  *{team}* [{sport}]",
        colour      = colour,
        timestamp   = datetime.now(timezone.utc),
    )
    embed.add_field(name="Status change", value=f"{old_s} → **{new_s}**", inline=False)
    embed.add_field(
        name="Action",
        value="Market odds may not have adjusted yet — run `/scan` for updated signals.",
        inline=False,
    )
    return embed


# ---------------------------------------------------------------------------
# Broadcast helper
# ---------------------------------------------------------------------------

async def _broadcast(bot: discord.Client, embed: discord.Embed, content: str = "") -> None:
    """Send an embed to every registered user channel."""
    chat_ids = _registry.get_all_chat_ids()
    sent = 0
    for cid in chat_ids:
        ch = bot.get_channel(cid)
        if ch is None:
            try:
                ch = await bot.fetch_channel(cid)
            except Exception:
                log.warning("_broadcast: can't find channel %d", cid)
                continue
        try:
            await ch.send(content=content or None, embed=embed)
            sent += 1
        except Exception as exc:
            log.warning("_broadcast failed for channel %d: %s", cid, exc)
    if not sent:
        log.warning("_broadcast: no channels registered yet")


# ---------------------------------------------------------------------------
# Session memory helper (per Discord user)
# ---------------------------------------------------------------------------

def _get_session(user_id: int) -> SessionMemory:
    return SessionMemory(user_id=user_id)


# ---------------------------------------------------------------------------
# AI response helper
# ---------------------------------------------------------------------------

_SMART_MONEY_TRIGGERS = {
    "trade", "buy", "bet", "position", "market", "edge", "wallet",
    "copy", "follow", "who", "smart money", "trader", "recommend",
    "should i", "worth", "streak", "hot", "specialist", "insider",
}


def _build_smart_money_context_discord() -> str:
    """Pull top tracked wallets and their open positions for AI context."""
    global _sm_positions_cache, _sm_cache_ts
    now = time.time()
    if _sm_positions_cache and (now - _sm_cache_ts) < _SM_CACHE_TTL:
        return _sm_positions_cache.get("block", "")
    try:
        traders = _tc.get_top(limit=5)
        if not traders:
            return ""
        lines = ["\n[Smart Money — top tracked wallets]"]
        for t in traders:
            addr  = t.get("wallet_address", "")[:10]
            score = t.get("final_score", 0)
            pnl   = t.get("pnl_alltime", 0)
            wr    = t.get("win_rate_alltime", 0)
            streak = int(t.get("current_streak", 0))
            streak_str = f" 🔥{streak}W" if streak >= 3 else ""
            lines.append(
                f"  Score {score:.0f}/100{streak_str} | "
                f"PnL ${pnl:+,.0f} | WR {wr:.0%} | {addr}..."
            )
        lines.append("[End Smart Money]")
        block = "\n".join(lines)
        _sm_positions_cache = {"block": block}
        _sm_cache_ts = now
        return block
    except Exception as exc:
        log.debug("smart_money_context_discord failed: %s", exc)
        return ""


async def _ai_reply(
    message: discord.Message,
    user_msg: str,
    user_id: int,
) -> str:
    """Build context and get an AI response — full feature parity with Telegram."""
    import time as _time
    t_start = _time.time()
    loop = asyncio.get_running_loop()

    mem      = _get_session(user_id)
    kb_ctx   = _kb.get_context_for_question(user_msg)
    sess_ctx = mem.get_session_context(max_exchanges=4)
    prof_ctx = _profiles.get_profile_context(user_id)

    # Recent scan opps
    svc = _get_service()
    top = svc.engine.top_opportunities(limit=3)
    scan_ctx = ""
    if top:
        lines = ["\nRecent scan opportunities:"]
        for r in top:
            lines.append(
                f"- {r.metadata.get('question', r.market_id)[:60]} "
                f"| market={r.market_prob:.0%} edge={r.edge:+.0%}"
            )
        scan_ctx = "\n".join(lines)

    # Smart money context — inject when trading keywords present
    q_lower = user_msg.lower()
    smart_money_ctx = ""
    if any(kw in q_lower for kw in _SMART_MONEY_TRIGGERS) or scan_ctx:
        smart_money_ctx = _build_smart_money_context_discord()

    # System prompt from registry (same as Telegram — chat_system@2.6)
    try:
        system_prompt, _pv = _prompt_registry.render(
            "chat_system",
            correction_instruction="",
            onboarding_hint="",
            user_name=str(user_id),
            current_month=datetime.now(timezone.utc).strftime("%B"),
        )
        # Discord uses markdown not HTML — append platform note
        system_prompt = system_prompt.replace(
            "operating on Telegram", "operating on Discord"
        ) + "\n\nUse Discord markdown (**bold**, *italic*, `code`) not HTML."
    except Exception:
        system_prompt = (
            "You are EDGE, an AI prediction market analyst on Discord. "
            "Help users find mispriced prediction markets. Be concise, under 300 words."
        )

    full_prompt = user_msg + kb_ctx + prof_ctx + sess_ctx + scan_ctx + smart_money_ctx

    reply = await loop.run_in_executor(
        None,
        lambda: get_chat_response(full_prompt, task_type="creative", system_prompt=system_prompt),
    )
    reply = reply or "Sorry, I couldn't generate a response right now."

    mem.add_exchange(user_msg, reply)
    _profiles.update_from_message(user_id, user_msg)

    # Log to decision log
    try:
        latency_ms = int((_time.time() - t_start) * 1000)
        ctx_blocks = ["kb", "session"]
        if smart_money_ctx:
            ctx_blocks.append("smart_money")
        if scan_ctx:
            ctx_blocks.append("scan")
        _decision_log.log(
            call_type="chat",
            model="discord",
            prompt_version="chat_system@2.6",
            context_blocks=ctx_blocks,
            latency_ms=latency_ms,
            user_id=str(user_id),
        )
    except Exception:
        pass

    return reply


# ---------------------------------------------------------------------------
# Scan runner
# ---------------------------------------------------------------------------

async def _run_scan(bot: discord.Client, notify: bool = True) -> str:
    global _last_status, _alerted_keys

    svc     = _get_service()
    scanner = _get_scanner()
    if scanner is None:
        return "Scanner not available"

    loop = asyncio.get_running_loop()
    try:
        inputs       = await loop.run_in_executor(None, scanner.collect)
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
            _alerted_keys.add(key)
            new_alerts += 1

            if notify and bot:
                embed = _embed_alert(rec)
                # Buttons for paper trading
                view = _AlertView(rec)
                chat_ids = _registry.get_all_chat_ids()
                for cid in chat_ids:
                    ch = bot.get_channel(cid)
                    if ch is None:
                        try:
                            ch = await bot.fetch_channel(cid)
                        except Exception:
                            continue
                    try:
                        await ch.send(embed=embed, view=view)
                    except Exception as exc:
                        log.warning("Scan alert send failed to %d: %s", cid, exc)

        _last_status = (
            f"Scan complete — {new_alerts} new alert(s) | "
            f"{summary.get('qualified', 0)} qualified of "
            f"{summary.get('total', 0)} markets"
        )
        return _last_status

    except Exception as exc:
        msg = f"Scan error: {exc}"
        log.error(msg, exc_info=True)
        return msg


# ---------------------------------------------------------------------------
# Paper trade button view
# ---------------------------------------------------------------------------

class _AlertView(discord.ui.View):
    def __init__(self, rec: Recommendation):
        super().__init__(timeout=None)
        self._rec = rec

    @discord.ui.button(label="✅ YES", style=discord.ButtonStyle.success, custom_id="pt_yes")
    async def yes_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, "YES")

    @discord.ui.button(label="❌ NO", style=discord.ButtonStyle.danger, custom_id="pt_no")
    async def no_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, "NO")

    @discord.ui.button(label="🔄 Fade", style=discord.ButtonStyle.secondary, custom_id="pt_fade")
    async def fade_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Take the opposite side of what the bot recommended."""
        rec       = self._rec
        bot_side  = "YES" if "YES" in (rec.action or "").upper() else "NO"
        fade_side = "NO"  if bot_side == "YES" else "YES"
        await self._handle(interaction, f"FADE_{fade_side}", fade_side=fade_side, bot_side=bot_side)

    @discord.ui.button(label="⏭ Skip", style=discord.ButtonStyle.secondary, custom_id="pt_skip")
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Skipped — no pick recorded.", ephemeral=True)

    async def _handle(
        self,
        interaction: discord.Interaction,
        side: str,
        fade_side: str | None = None,
        bot_side:  str | None = None,
    ):
        uid   = interaction.user.id
        rec   = self._rec
        prob  = rec.market_prob or 0.5
        stake = 10.0
        ok    = _ot.record_user_pick(
            user_id      = uid,
            signal_id    = rec.market_id,
            market_id    = rec.market_id,
            side         = side,           # "YES" | "NO" | "FADE_YES" | "FADE_NO"
            paper_stake  = stake,
            entry_prob   = prob,
        )
        if ok:
            if fade_side:
                # Fade: price flips to the opposite side
                f_prob = (1 - prob) if fade_side == "YES" else prob
                payout = round(stake * (1 / max(f_prob, 0.01) - 1), 2)
                await interaction.response.send_message(
                    f"🔄 **Fade logged** — you took **{fade_side}** against the bot's {bot_side}\n"
                    f"Paper $10 @ {f_prob:.0%} — Win = **+${payout:.2f}** | Loss = -$10.00\n"
                    f"EDGE will track resolution automatically.",
                    ephemeral=True,
                )
            else:
                payout = round(stake * (1 / prob - 1), 2) if side == "YES" else round(stake * (1 / (1 - prob) - 1), 2)
                await interaction.response.send_message(
                    f"📝 Paper trade logged — **{side}** $10 @ {prob:.0%}\n"
                    f"Potential payout: **${payout:.2f}**",
                    ephemeral=True,
                )
        else:
            await interaction.response.send_message(
                "You already picked this one!", ephemeral=True
            )


# ---------------------------------------------------------------------------
# Discord Bot class
# ---------------------------------------------------------------------------

class EdgeDiscordBot(discord.Client):
    def __init__(self):
        intents                  = discord.Intents.default()
        intents.message_content  = True
        intents.members          = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        log.info("Slash commands synced to guild %d", GUILD_ID)

    async def on_ready(self) -> None:
        log.info("EDGE Discord bot ready — logged in as %s", self.user)
        if not scan_job.is_running():
            scan_job.start()
        if not injury_job.is_running():
            injury_job.start()
        if not resolution_job.is_running():
            resolution_job.start()
        if not smart_money_job.is_running():
            smart_money_job.start()
        if not insider_job.is_running():
            insider_job.start()

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        # Only respond in registered user channels (private 1-on-1 channels)
        uid       = message.author.id
        chat_ids  = _registry.get_all_chat_ids()
        if message.channel.id not in chat_ids:
            return
        # Must be an allowed user
        if not _registry.is_allowed(uid):
            return
        # Skip messages that start with / (slash commands handle those)
        if message.content.startswith("/"):
            return

        async with message.channel.typing():
            reply = await _ai_reply(message, message.content, uid)
            # Discord has 2000 char limit — split if needed
            if len(reply) <= 2000:
                await message.channel.send(reply)
            else:
                chunks = [reply[i:i+1990] for i in range(0, len(reply), 1990)]
                for chunk in chunks:
                    await message.channel.send(chunk)


bot = EdgeDiscordBot()


# ---------------------------------------------------------------------------
# Background jobs
# ---------------------------------------------------------------------------

@tasks.loop(minutes=SCAN_INTERVAL_MIN)
async def scan_job():
    log.info("Running scheduled scan...")
    result = await _run_scan(bot, notify=True)
    log.info("Scan result: %s", result.split("\n")[0])


@tasks.loop(minutes=INJURY_REFRESH_MIN)
async def injury_job():
    """Refresh injury cache and broadcast any status changes."""
    try:
        _injury_api = importlib.import_module("edge_agent.dat-ingestion.injury_api")
        from edge_agent.memory.injury_cache import InjuryCache

        cache = InjuryCache()
        sports = ("nba", "nfl", "nhl", "cfb", "cbb", "wnba", "ncaaw")

        for sport in sports:
            try:
                fresh = await asyncio.get_running_loop().run_in_executor(
                    None, lambda s=sport: _injury_api.fetch_and_store(s)
                )
                if not fresh:
                    continue

                changes = cache.get_changes(sport)
                for chg in changes:
                    player  = chg.get("player", "")
                    team    = chg.get("team", "")
                    old_s   = chg.get("old_status", "")
                    new_s   = chg.get("new_status", "")
                    pos     = chg.get("position", "")
                    direction = chg.get("direction", "worsening")

                    embed = _embed_injury(
                        player, team, sport.upper(),
                        old_s, new_s, direction, pos,
                    )
                    await _broadcast(bot, embed)

            except Exception as exc:
                log.warning("Injury refresh failed for %s: %s", sport, exc)

    except Exception as exc:
        log.error("injury_job error: %s", exc, exc_info=True)


@tasks.loop(minutes=30)
async def resolution_job():
    """Resolve pending paper trade picks."""
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _ot.resolve_pending)
    except Exception as exc:
        log.warning("resolution_job error: %s", exc)


@tasks.loop(minutes=30)
async def smart_money_job():
    """Refresh smart money cache and broadcast copy-trade alerts for new positions."""
    global _sm_positions_cache, _sm_cache_ts, _sm_prev_positions
    _sm_cache_ts = 0  # force refresh
    try:
        traders = _tc.get_top(limit=5)
        if not traders:
            return
        _trader_api = importlib.import_module("edge_agent.dat-ingestion.trader_api")
        loop = asyncio.get_running_loop()
        new_positions: dict = {}
        for t in traders:
            addr  = t.get("wallet_address", "")
            score = t.get("final_score", 0)
            if score < 35 or not addr:
                continue
            try:
                positions = await loop.run_in_executor(
                    None, lambda a=addr: _trader_api.get_open_positions(a)
                )
                for p in (positions or []):
                    cid  = p.get("conditionId", "")
                    size = float(p.get("currentValue") or p.get("size") or 0)
                    if size < 100 or not cid:
                        continue
                    price = float(p.get("currentPrice") or p.get("price") or 0)
                    # Filter: skip near-certain and too-late entries
                    if price > 0.80 or price < 0.15:
                        continue
                    key = f"{addr}:{cid}"
                    new_positions[key] = {"addr": addr, "cid": cid, "size": size,
                                          "price": price, "score": score,
                                          "q": p.get("question", "")[:80]}
            except Exception:
                continue

        # Detect new positions not in previous cache
        truly_new = {k: v for k, v in new_positions.items() if k not in _sm_prev_positions}
        _sm_prev_positions = new_positions

        if truly_new:
            chat_ids = _registry.get_all_chat_ids()
            for pos in truly_new.values():
                embed = discord.Embed(
                    title  = "🐋 Copy-Trade Alert — Smart Money Move",
                    colour = 0xAA00FF,
                    timestamp = datetime.now(timezone.utc),
                )
                embed.add_field(name="Market",  value=pos["q"] or pos["cid"][:40], inline=False)
                embed.add_field(name="Score",   value=f"{pos['score']:.0f}/100",   inline=True)
                embed.add_field(name="Size",    value=f"${pos['size']:,.0f}",       inline=True)
                embed.add_field(name="Price",   value=f"{int(pos['price']*100)}% YES", inline=True)
                embed.add_field(name="Wallet",  value=f"`{pos['addr'][:10]}...`",  inline=False)
                embed.set_footer(text="Entry window open — verify on Polymarket before following")
                for cid in chat_ids:
                    ch = bot.get_channel(cid)
                    if ch:
                        try:
                            await ch.send(embed=embed)
                        except Exception:
                            pass
    except Exception as exc:
        log.warning("smart_money_job error: %s", exc)


@tasks.loop(minutes=5)
async def insider_job():
    """Detect whale/insider bets on niche markets and broadcast to all user channels."""
    engine = _get_insider_engine()
    try:
        chat_ids = _registry.get_all_chat_ids()

        async def _send_to_channels(msg: str) -> None:
            embed = discord.Embed(
                description = msg[:4000],
                colour      = 0xFF4500,
                timestamp   = datetime.now(timezone.utc),
            )
            embed.set_author(name="🚨 Insider Alert — Whale Detected")
            for cid in chat_ids:
                ch = bot.get_channel(cid)
                if ch:
                    try:
                        await ch.send(embed=embed)
                    except Exception:
                        pass

        n = await engine.run_scan(send_alert_fn=_send_to_channels)
        if n:
            log.info("[insider_job] %d alert(s) fired", n)
    except Exception as exc:
        log.warning("insider_job error: %s", exc)


# ---------------------------------------------------------------------------
# Admin slash commands (owner only)
# ---------------------------------------------------------------------------

def _is_owner(interaction: discord.Interaction) -> bool:
    return interaction.user.id == OWNER_ID


@bot.tree.command(name="adduser", description="[Admin] Add a paying user and create their private channel")
@app_commands.describe(member="The Discord member to add")
async def cmd_adduser(interaction: discord.Interaction, member: discord.Member):
    if not _is_owner(interaction):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    guild    = interaction.guild
    category = guild.get_channel(CATEGORY_ID) if CATEGORY_ID else None

    # Permission overwrites: private to this user + bot
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        member:             discord.PermissionOverwrite(
            read_messages=True, send_messages=True, read_message_history=True
        ),
        guild.me:           discord.PermissionOverwrite(
            read_messages=True, send_messages=True, embed_links=True
        ),
    }

    # Channel name: edge-username (lowercase, no spaces)
    ch_name = f"edge-{member.name.lower().replace(' ', '-')}"

    try:
        channel = await guild.create_text_channel(
            name      = ch_name,
            category  = category,
            overwrites= overwrites,
            topic     = f"Private EDGE channel for {member.display_name}",
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "❌ Bot missing 'Manage Channels' permission.", ephemeral=True
        )
        return
    except Exception as exc:
        await interaction.followup.send(f"❌ Channel creation failed: {exc}", ephemeral=True)
        return

    # Register in the channel registry
    _registry.add_user(member.id, username=member.name, added_by=interaction.user.id)
    _registry.register(
        user_id    = member.id,
        chat_id    = channel.id,
        username   = member.name,
        first_name = member.display_name,
    )

    # Welcome message in the new channel
    embed = discord.Embed(
        title       = "👋 Welcome to EDGE",
        description = (
            f"Hey {member.mention}! Your private prediction market intelligence channel is ready.\n\n"
            "**What I do:**\n"
            "• Scan Polymarket & Kalshi for mispriced markets\n"
            "• Vet and track smart money wallets — copy-trade alerts fire here\n"
            "• Detect whale/insider bets on niche markets before they move\n"
            "• Track injuries and their market impact in real-time\n"
            "• Build your paper trading track record with one tap\n\n"
            "**Quick start:**\n"
            "`/scan` — live market scan\n"
            "`/top` — top 3 highest-EV picks\n"
            "`/traders` — top smart money wallets\n"
            "`/insider` — recent whale/insider alerts\n"
            "`/injuries nba` — live injury report\n"
            "`/mytrades` — your paper P&L\n"
            "`/watch <address>` — add a wallet to watchlist\n\n"
            "**Auto-alerts (no action needed):**\n"
            "🐋 Copy-trade: smart money opens a new position\n"
            "🚨 Insider: fresh wallet places large bet on niche market\n"
            "🚑 Injury: player status changes before market adjusts\n\n"
            "Or just type anything — I'm always listening here."
        ),
        colour    = 0x5865F2,
        timestamp = datetime.now(timezone.utc),
    )
    embed.set_footer(text="EDGE — Prediction Market Intelligence")
    await channel.send(member.mention, embed=embed)

    log.info("Created channel #%s (id=%d) for user %s (%d)", ch_name, channel.id, member.name, member.id)
    await interaction.followup.send(
        f"✅ Created {channel.mention} for {member.mention}.", ephemeral=True
    )


@bot.tree.command(name="removeuser", description="[Admin] Remove a user and delete their channel")
@app_commands.describe(member="The Discord member to remove")
async def cmd_removeuser(interaction: discord.Interaction, member: discord.Member):
    if not _is_owner(interaction):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    # Find and delete their registered channel
    chat_id = _registry.get_chat_id(member.id)
    if chat_id:
        ch = interaction.guild.get_channel(chat_id)
        if ch:
            try:
                await ch.delete(reason=f"EDGE user removed: {member.name}")
            except Exception as exc:
                log.warning("Could not delete channel %d: %s", chat_id, exc)

    _registry.remove_user(member.id)
    log.info("Removed user %s (%d) and their channel", member.name, member.id)
    await interaction.followup.send(
        f"🗑 Removed {member.mention} and deleted their channel.", ephemeral=True
    )


@bot.tree.command(name="listusers", description="[Admin] Show all registered users")
async def cmd_listusers(interaction: discord.Interaction):
    if not _is_owner(interaction):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return

    allowed    = _registry.list_allowed()
    registered = {u["user_id"]: u for u in _registry.get_registered_users()}

    if not allowed:
        await interaction.response.send_message("No users yet. Use /adduser to add someone.", ephemeral=True)
        return

    embed = discord.Embed(title="👥 EDGE User Roster", colour=0x5865F2)
    for u in allowed:
        uid  = u["user_id"]
        name = f"@{u['username']}" if u.get("username") else str(uid)
        reg  = registered.get(uid)
        if reg:
            last = (reg.get("last_seen") or "")[:10]
            val  = f"✅ Channel registered · last active {last}"
        else:
            val = "⏳ Allowed but hasn't started yet"
        embed.add_field(name=f"{name} ({uid})", value=val, inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# User slash commands
# ---------------------------------------------------------------------------

@bot.tree.command(name="scan", description="Run a live market scan for edge opportunities")
async def cmd_scan(interaction: discord.Interaction):
    if not _registry.is_allowed(interaction.user.id):
        await interaction.response.send_message("❌ Not authorised.", ephemeral=True)
        return
    await interaction.response.defer()
    result = await _run_scan(bot, notify=True)
    await interaction.followup.send(f"```\n{result[:1900]}\n```")


@bot.tree.command(name="top", description="Top 3 highest-EV opportunities right now")
async def cmd_top(interaction: discord.Interaction):
    if not _registry.is_allowed(interaction.user.id):
        await interaction.response.send_message("❌ Not authorised.", ephemeral=True)
        return
    await interaction.response.defer()
    svc = _get_service()
    top = svc.engine.top_opportunities(limit=3)
    if not top:
        await interaction.followup.send("No qualified opportunities in cache — run `/scan` first.")
        return
    embeds = [_embed_alert(r) for r in top]
    await interaction.followup.send(embeds=embeds[:3])


@bot.tree.command(name="status", description="Last scan summary")
async def cmd_status(interaction: discord.Interaction):
    if not _registry.is_allowed(interaction.user.id):
        await interaction.response.send_message("❌ Not authorised.", ephemeral=True)
        return
    await interaction.response.send_message(f"```\n{_last_status}\n```")


@bot.tree.command(name="injuries", description="Live injury report")
@app_commands.describe(sport="Sport: nba, nfl, nhl, cfb, cbb, wnba, ncaaw")
async def cmd_injuries(interaction: discord.Interaction, sport: str = "nba"):
    if not _registry.is_allowed(interaction.user.id):
        await interaction.response.send_message("❌ Not authorised.", ephemeral=True)
        return
    await interaction.response.defer()
    sport = sport.lower().strip()
    _VALID = {"nba", "nfl", "nhl", "cfb", "cbb", "wnba", "ncaaw"}
    if sport not in _VALID:
        await interaction.followup.send(f"Valid sports: {', '.join(sorted(_VALID))}")
        return
    try:
        from edge_agent.memory.injury_cache import InjuryCache
        records = InjuryCache().get_all(sport)
        if not records:
            await interaction.followup.send(f"No {sport.upper()} injury data cached. Refreshing...")
            return
        _SEV_ICON = {"Out": "🔴", "Doubtful": "🟠", "Questionable": "🟡",
                     "Day-To-Day": "🟡", "Active": "🟢"}
        lines = [f"**{sport.upper()} Injury Report**\n"]
        for r in sorted(records, key=lambda x: x.get("status", "")):
            icon = _SEV_ICON.get(r.get("status", ""), "⚪")
            lines.append(
                f"{icon} **{r.get('player', '?')}** ({r.get('team', '?')}) — "
                f"{r.get('status', '?')}"
                + (f" · {r.get('injury', '')}" if r.get("injury") else "")
            )
        text = "\n".join(lines)
        if len(text) > 1900:
            text = text[:1900] + "\n…(truncated)"
        await interaction.followup.send(text)
    except Exception as exc:
        await interaction.followup.send(f"Error: {exc}")


@bot.tree.command(name="traders", description="Top Polymarket smart money traders")
async def cmd_traders(interaction: discord.Interaction):
    if not _registry.is_allowed(interaction.user.id):
        await interaction.response.send_message("❌ Not authorised.", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        _trader_mod = importlib.import_module("edge_agent.dat-ingestion.trader_api")
        from edge_agent.memory.trader_cache import TraderCache
        tc      = TraderCache()
        traders = tc.get_top(limit=10)
        if not traders:
            await interaction.followup.send("No trader data cached. Will populate on next refresh.")
            return
        embed = discord.Embed(
            title     = "🐋 Smart Money — Top Traders",
            colour    = 0xAA00FF,
            timestamp = datetime.now(timezone.utc),
        )
        for i, t in enumerate(traders[:10], 1):
            name   = t.get("username") or t.get("address", "?")[:12]
            score  = t.get("score", 0)
            pnl7   = t.get("pnl_7d", 0)
            pnl30  = t.get("pnl_30d", 0)
            wins   = t.get("win_count", 0)
            embed.add_field(
                name  = f"#{i} {name}  —  {score}/100",
                value = f"7d: ${pnl7:+,.0f}  |  30d: ${pnl30:+,.0f}  |  {wins}W",
                inline= False,
            )
        await interaction.followup.send(embed=embed)
    except Exception as exc:
        await interaction.followup.send(f"Error fetching traders: {exc}")


@bot.tree.command(name="wallet", description="Vet a Polymarket wallet address")
@app_commands.describe(address="Wallet address or username")
async def cmd_wallet(interaction: discord.Interaction, address: str):
    if not _registry.is_allowed(interaction.user.id):
        await interaction.response.send_message("❌ Not authorised.", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        _trader_mod = importlib.import_module("edge_agent.dat-ingestion.trader_api")
        loop   = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, lambda: _trader_mod.vet_wallet(address)
        )
        embed  = discord.Embed(
            title     = f"🔍 Wallet Vet — {address[:20]}",
            colour    = 0x2196F3,
            timestamp = datetime.now(timezone.utc),
        )
        for k, v in result.items():
            embed.add_field(name=k, value=str(v)[:200], inline=True)
        await interaction.followup.send(embed=embed)
    except Exception as exc:
        await interaction.followup.send(f"Error vetting wallet: {exc}")


@bot.tree.command(name="performance", description="Your paper P&L and signal win rate")
@app_commands.describe(days="Lookback in days (default 30)")
async def cmd_performance(interaction: discord.Interaction, days: int = 30):
    if not _registry.is_allowed(interaction.user.id):
        await interaction.response.send_message("❌ Not authorised.", ephemeral=True)
        return
    await interaction.response.defer()
    uid  = interaction.user.id
    pnl  = _ot.user_pnl(user_id=uid, days=days)
    svc  = _get_service()
    sig  = svc.engine.signal_performance()

    embed = discord.Embed(
        title     = f"📊 Performance — Last {days} Days",
        colour    = 0x5865F2,
        timestamp = datetime.now(timezone.utc),
    )
    embed.add_field(name="Your Paper P&L",  value=f"${pnl.get('total_pnl', 0):+,.2f}", inline=True)
    embed.add_field(name="Win Rate",        value=f"{pnl.get('win_rate', 0):.0%}",      inline=True)
    embed.add_field(name="ROI",             value=f"{pnl.get('roi', 0):+.1%}",          inline=True)
    embed.add_field(name="Total Picks",     value=str(pnl.get("total_picks", 0)),        inline=True)
    embed.add_field(name="Settled",         value=str(pnl.get("wins", 0) + pnl.get("losses", 0)), inline=True)
    embed.add_field(name="Pending",         value=str(pnl.get("pending", 0)),            inline=True)

    if sig:
        lines = []
        for s, d in sig.items():
            lines.append(f"**{s}**: {d.get('win_rate', 0):.0%} WR  ({d.get('count', 0)} signals)")
        embed.add_field(name="🤖 EDGE Signal Performance", value="\n".join(lines[:6]) or "No data yet", inline=False)

    await interaction.followup.send(embed=embed)


@bot.tree.command(name="mytrades", description="Your open paper picks + settled history")
async def cmd_mytrades(interaction: discord.Interaction):
    if not _registry.is_allowed(interaction.user.id):
        await interaction.response.send_message("❌ Not authorised.", ephemeral=True)
        return
    await interaction.response.defer()
    uid   = interaction.user.id
    picks = _ot.get_user_picks(user_id=uid, limit=20)

    if not picks:
        await interaction.followup.send(
            "No paper trades yet! Tap **✅ YES** or **❌ NO** on any scan alert to log a pick."
        )
        return

    embed = discord.Embed(
        title     = "📋 My Paper Trades",
        colour    = 0x5865F2,
        timestamp = datetime.now(timezone.utc),
    )
    _OUTCOME_ICON = {"WIN": "✅", "LOSS": "❌", "VOID": "⬛", "PENDING": "⏳"}
    for p in picks[:15]:
        icon    = _OUTCOME_ICON.get(p.get("pick_outcome", "PENDING"), "⏳")
        q       = (p.get("question") or p.get("market_id") or "?")[:50]
        side    = p.get("side", "?")
        stake   = p.get("paper_stake", 10)
        pnl_val = p.get("paper_pnl")
        pnl_str = f"  P&L: **${pnl_val:+.2f}**" if pnl_val is not None else ""
        embed.add_field(
            name  = f"{icon} {side}  ${stake:.0f}",
            value = f"{q}{pnl_str}",
            inline= False,
        )

    pnl = _ot.user_pnl(user_id=uid)
    embed.set_footer(
        text=(
            f"Total P&L: ${pnl.get('total_pnl', 0):+.2f}  |  "
            f"Win rate: {pnl.get('win_rate', 0):.0%}  |  "
            f"{pnl.get('pending', 0)} pending"
        )
    )
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="standings", description="League standings + championship odds")
@app_commands.describe(sport="Sport: nba, nfl, nhl, nba, laliga, bundesliga, f1, pga, etc.")
async def cmd_standings(interaction: discord.Interaction, sport: str = "nba"):
    if not _registry.is_allowed(interaction.user.id):
        await interaction.response.send_message("❌ Not authorised.", ephemeral=True)
        return
    await interaction.response.defer()
    if _standings_mod is None:
        await interaction.followup.send("Standings module not available.")
        return
    try:
        loop    = asyncio.get_running_loop()
        client  = _standings_mod.StandingsClient()
        text    = await loop.run_in_executor(None, lambda: client.format_standings(sport.lower()))
        if len(text) > 1900:
            text = text[:1900] + "\n…(truncated, use /standings for full table)"
        await interaction.followup.send(f"```\n{text}\n```")
    except Exception as exc:
        await interaction.followup.send(f"Error: {exc}")


# ---------------------------------------------------------------------------
# Watchlist commands
# ---------------------------------------------------------------------------

@bot.tree.command(name="watch", description="Add a Polymarket wallet to your watchlist")
@app_commands.describe(address="Wallet address to watch")
async def cmd_watch(interaction: discord.Interaction, address: str):
    if not _registry.is_allowed(interaction.user.id):
        await interaction.response.send_message("❌ Not authorised.", ephemeral=True)
        return
    try:
        added = _tc.watchlist_add(address.strip(), added_by="discord_user", note="Added via /watch")
        if added:
            await interaction.response.send_message(
                f"✅ Added `{address[:20]}...` to watchlist. Will be fully vetted within 6h.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"ℹ️ `{address[:20]}...` is already on the watchlist.", ephemeral=True
            )
    except Exception as exc:
        await interaction.response.send_message(f"❌ Error: {exc}", ephemeral=True)


@bot.tree.command(name="unwatch", description="Remove a wallet from your watchlist")
@app_commands.describe(address="Wallet address to remove")
async def cmd_unwatch(interaction: discord.Interaction, address: str):
    if not _registry.is_allowed(interaction.user.id):
        await interaction.response.send_message("❌ Not authorised.", ephemeral=True)
        return
    try:
        _tc.watchlist_remove(address.strip())
        await interaction.response.send_message(
            f"🗑 Removed `{address[:20]}...` from watchlist.", ephemeral=True
        )
    except Exception as exc:
        await interaction.response.send_message(f"❌ Error: {exc}", ephemeral=True)


@bot.tree.command(name="watchlist", description="Show all wallets on your watchlist")
async def cmd_watchlist(interaction: discord.Interaction):
    if not _registry.is_allowed(interaction.user.id):
        await interaction.response.send_message("❌ Not authorised.", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        wallets = _tc.watchlist_list()
        if not wallets:
            await interaction.followup.send(
                "Watchlist is empty. Use `/watch <address>` to add wallets."
            )
            return
        embed = discord.Embed(
            title     = "👁 Watchlist",
            colour    = 0xAA00FF,
            timestamp = datetime.now(timezone.utc),
        )
        for w in wallets[:20]:
            addr  = w.get("address", "")
            score = w.get("final_score") or w.get("fast_score") or 0
            note  = w.get("note", "")[:50]
            embed.add_field(
                name  = f"`{addr[:10]}...{addr[-4:]}`  —  {score:.0f}/100",
                value = note or "No note",
                inline= False,
            )
        await interaction.followup.send(embed=embed)
    except Exception as exc:
        await interaction.followup.send(f"Error: {exc}")


# ---------------------------------------------------------------------------
# Tracking command
# ---------------------------------------------------------------------------

@bot.tree.command(name="tracking", description="Show live game tracking list (injury-triggered)")
async def cmd_tracking(interaction: discord.Interaction):
    if not _registry.is_allowed(interaction.user.id):
        await interaction.response.send_message("❌ Not authorised.", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        from edge_agent.game_tracker import GameTracker
        loop    = asyncio.get_running_loop()
        tracker = GameTracker()
        games   = await loop.run_in_executor(None, tracker.get_tracked_games)
        if not games:
            await interaction.followup.send("No games currently being tracked.")
            return
        embed = discord.Embed(
            title     = "👁 Game Tracker — Active",
            colour    = 0xFF8800,
            timestamp = datetime.now(timezone.utc),
        )
        for g in games[:15]:
            drop      = g.get("current_drop", 0)
            triggered = g.get("triggered", False)
            status    = "🔥 TRIGGERED" if triggered else f"drop {drop:+.1%}"
            phase     = g.get("phase", "")
            q         = g.get("question", "?")[:60]
            embed.add_field(
                name  = f"{'🔥' if triggered else '👁'} [{phase}] {status}",
                value = q,
                inline= False,
            )
        await interaction.followup.send(embed=embed)
    except Exception as exc:
        await interaction.followup.send(f"Error: {exc}")


# ---------------------------------------------------------------------------
# Insider alerts command
# ---------------------------------------------------------------------------

@bot.tree.command(name="insider", description="Recent insider/whale bet alerts")
@app_commands.describe(limit="Number of alerts to show (default 10)")
async def cmd_insider(interaction: discord.Interaction, limit: int = 10):
    if not _registry.is_allowed(interaction.user.id):
        await interaction.response.send_message("❌ Not authorised.", ephemeral=True)
        return
    await interaction.response.defer()
    engine  = _get_insider_engine()
    alerts  = engine.get_recent_alerts(limit=min(limit, 20))
    if not alerts:
        await interaction.followup.send(
            "No insider alerts yet. The engine scans every 5 minutes for fresh "
            "wallets placing large bets on niche markets."
        )
        return
    embed = discord.Embed(
        title     = f"🚨 Insider Alerts — Last {len(alerts)}",
        colour    = 0xFF4500,
        timestamp = datetime.now(timezone.utc),
    )
    for a in alerts:
        ts      = datetime.fromtimestamp(a["fired_at"], tz=timezone.utc).strftime("%m/%d %H:%M")
        addr    = a["wallet"][:6] + "..." + a["wallet"][-4:]
        score   = a["suspicion_score"]
        size    = a["trade_size_usd"]
        price   = int(a["current_price"] * 100)
        outcome = a["outcome"]
        icon    = {"win": "✅", "loss": "❌", "pending": "⏳"}.get(outcome, "⏳")
        s_icon  = "🚨" if score >= 70 else "⚠️" if score >= 50 else "🔍"
        embed.add_field(
            name  = f"{s_icon} {score}/100  —  ${size:,.0f} @ {price}% YES  {icon}",
            value = f"`{addr}`  ·  {a['question'][:60]}  ·  {ts}",
            inline= False,
        )
    embed.set_footer(text="70+ = HIGH | 50-69 = MEDIUM-HIGH | 45-49 = MEDIUM")
    await interaction.followup.send(embed=embed)


# ---------------------------------------------------------------------------
# ML status command
# ---------------------------------------------------------------------------

@bot.tree.command(name="mlstatus", description="[Admin] ML model status and calibration info")
async def cmd_mlstatus(interaction: discord.Interaction):
    if not _is_owner(interaction):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        loop    = asyncio.get_running_loop()
        labeled = await loop.run_in_executor(
            None, lambda: _ml_store.get_labeled_features(min_samples=0, days=180)
        )
        n_total = len(labeled)
        n_30d   = len([x for x in labeled if x.get("age_days", 999) <= 30])
        cal_ok  = _calibrator.is_trained
        xgb_ok  = _signal_scorer.is_trained
        phase   = getattr(_signal_scorer, "_phase", 0)
        drifted = False
        try:
            recent  = _ml_store.get_labeled_features(min_samples=0, days=14)
            drifted = _regime.check(recent)
        except Exception:
            pass

        embed = discord.Embed(
            title     = "🤖 ML Status",
            colour    = 0x5865F2,
            timestamp = datetime.now(timezone.utc),
        )
        embed.add_field(name="Labeled signals",       value=f"{n_total} total / {n_30d} last 30d", inline=False)
        embed.add_field(name="Calibrator",            value="✅ Trained" if cal_ok else "⏳ Not trained (needs 50+)", inline=True)
        embed.add_field(name="XGBoost scorer",        value=f"✅ Phase {phase}" if xgb_ok else "⏳ Shadow mode (<400)", inline=True)
        embed.add_field(name="Regime drift",          value="🔴 DRIFT DETECTED" if drifted else "✅ Stable", inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"Error: {exc}", ephemeral=True)


# ---------------------------------------------------------------------------
# Decision log command
# ---------------------------------------------------------------------------

@bot.tree.command(name="decisions", description="[Admin] Last AI decisions for debugging")
@app_commands.describe(limit="Number of decisions to show (default 10)")
async def cmd_decisions(interaction: discord.Interaction, limit: int = 10):
    if not _is_owner(interaction):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        rows = _decision_log.get_recent(limit=min(limit, 20))
        if not rows:
            await interaction.followup.send("No decisions logged yet.", ephemeral=True)
            return
        embed = discord.Embed(
            title     = f"🔍 Last {len(rows)} AI Decisions",
            colour    = 0x5865F2,
            timestamp = datetime.now(timezone.utc),
        )
        for r in rows:
            ts      = datetime.fromtimestamp(r.get("created_at", 0), tz=timezone.utc).strftime("%m/%d %H:%M")
            model   = r.get("model", "?")
            pv      = r.get("prompt_version", "?")
            latency = r.get("latency_ms", 0)
            ctx     = r.get("context_blocks", "")
            embed.add_field(
                name  = f"`{pv}` — {model} — {latency}ms — {ts}",
                value = f"ctx: {ctx or 'none'}",
                inline= False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"Error: {exc}", ephemeral=True)


# ---------------------------------------------------------------------------
# Approvals command (admin)
# ---------------------------------------------------------------------------

@bot.tree.command(name="approvals", description="[Admin] Manage approved signal types for alerts")
@app_commands.describe(action="add / remove / list", signal="Signal type e.g. MISPRICED_YES")
async def cmd_approvals(interaction: discord.Interaction, action: str = "list", signal: str = ""):
    if not _is_owner(interaction):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return
    global _approved_signals
    action = action.lower().strip()
    signal = signal.upper().strip()
    if action == "add" and signal:
        _approved_signals.add(signal)
        _APPROVALS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _APPROVALS_FILE.write_text(json.dumps(sorted(_approved_signals)))
        await interaction.response.send_message(f"✅ Added `{signal}` to approved signals.", ephemeral=True)
    elif action == "remove" and signal:
        _approved_signals.discard(signal)
        _APPROVALS_FILE.write_text(json.dumps(sorted(_approved_signals)))
        await interaction.response.send_message(f"🗑 Removed `{signal}` from approved signals.", ephemeral=True)
    else:
        sigs = ", ".join(sorted(_approved_signals)) if _approved_signals else "All (no filter active)"
        await interaction.response.send_message(
            f"**Approved signals:** {sigs}\n\nValid types: `MISPRICED_YES`, `MISPRICED_NO`, `MOMENTUM`, `SMART_MONEY`, `INJURY_MOMENTUM_REVERSAL`",
            ephemeral=True,
        )


@bot.tree.command(name="weatherscan", description="Scan weather markets vs Open-Meteo 7-day forecast")
async def cmd_weatherscan(interaction: discord.Interaction):
    if not _registry.is_allowed(interaction.user.id):
        await interaction.response.send_message("❌ Not authorised.", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        from edge_agent.scanners.weather_scanner import scan_weather_markets
        markets = await asyncio.get_running_loop().run_in_executor(None, _fetch_all_open_markets_discord)
        gaps    = await asyncio.get_running_loop().run_in_executor(None, scan_weather_markets, markets)
        if not gaps:
            await interaction.followup.send(
                "✅ No weather market gaps detected (all within 15pp of Open-Meteo model)."
            )
            return
        lines = [f"**🌤️ Weather Market Gaps — {len(gaps)} found**\n"]
        for g in gaps[:5]:
            cond = {"temp_above": "🌡️", "temp_below": "🥶", "snow": "❄️", "rain": "🌧️"}.get(g.condition, "🌤️")
            act  = "📈 BUY YES" if g.action == "BUY YES" else "📉 BUY NO"
            lines.append(
                f"{cond} **{g.title[:65]}**\n"
                f"  Market {g.market_prob:.0%} → Model {g.model_prob:.0%} ({g.gap_pp:+.0f}pp) → {act}\n"
                f"  📍 {g.city} | {g.forecast_summary}\n"
            )
        await interaction.followup.send("\n".join(lines)[:1900])
    except Exception as exc:
        await interaction.followup.send(f"⚠️ Weather scan error: {exc}")


@bot.tree.command(name="cryptoscan", description="Scan crypto markets vs Binance lognormal model")
async def cmd_cryptoscan(interaction: discord.Interaction):
    if not _registry.is_allowed(interaction.user.id):
        await interaction.response.send_message("❌ Not authorised.", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        from edge_agent.scanners.crypto_scanner import scan_crypto_markets
        markets = await asyncio.get_running_loop().run_in_executor(None, _fetch_all_open_markets_discord)
        gaps    = await asyncio.get_running_loop().run_in_executor(None, scan_crypto_markets, markets)
        if not gaps:
            await interaction.followup.send(
                "✅ No crypto market gaps detected (all within 15pp of lognormal model)."
            )
            return
        lines = [f"**₿ Crypto Market Gaps — {len(gaps)} found**\n"]
        for g in gaps[:5]:
            sym = g.symbol.replace("USDT", "")
            act = "📈 BUY YES" if g.action == "BUY YES" else "📉 BUY NO"
            lines.append(
                f"**{g.title[:65]}**\n"
                f"  {sym}: ${g.current_price:,.2f} | 24h {g.change_24h:+.1f}%\n"
                f"  Market {g.market_prob:.0%} → Model {g.model_prob:.0%} ({g.gap_pp:+.0f}pp) → {act}\n"
            )
        await interaction.followup.send("\n".join(lines)[:1900])
    except Exception as exc:
        await interaction.followup.send(f"⚠️ Crypto scan error: {exc}")


@bot.tree.command(name="fedscan", description="Scan Fed/econ markets vs NY Fed + Treasury yield curve")
async def cmd_fedscan(interaction: discord.Interaction):
    if not _registry.is_allowed(interaction.user.id):
        await interaction.response.send_message("❌ Not authorised.", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        from edge_agent.scanners.econ_scanner import scan_econ_markets, get_econ_context_string
        econ_ctx = await asyncio.get_running_loop().run_in_executor(None, get_econ_context_string)
        markets  = await asyncio.get_running_loop().run_in_executor(None, _fetch_all_open_markets_discord)
        gaps     = await asyncio.get_running_loop().run_in_executor(None, scan_econ_markets, markets)
        cat_emoji = {"fed_rate": "🏦", "inflation": "📈", "recession": "📉",
                     "unemployment": "👷", "gdp": "📊"}
        lines = ["**🏛️ Fed / Econ Market Scan**\n"]
        if econ_ctx:
            lines.append(f"```\n{econ_ctx}\n```\n")
        if not gaps:
            lines.append("✅ No econ market gaps detected (all within 15pp of yield curve model).")
        else:
            lines.append(f"**{len(gaps)} gap(s) found:**\n")
            for g in gaps[:5]:
                ce  = cat_emoji.get(g.category, "🏛️")
                act = "📈 BUY YES" if g.action == "BUY YES" else "📉 BUY NO"
                lines.append(
                    f"{ce} **{g.title[:65]}**\n"
                    f"  Market {g.market_prob:.0%} → Model {g.model_prob:.0%} ({g.gap_pp:+.0f}pp) → {act}\n"
                    f"  *{g.signal_notes[:80]}*\n"
                )
        await interaction.followup.send("\n".join(lines)[:1900])
    except Exception as exc:
        await interaction.followup.send(f"⚠️ Fed scan error: {exc}")


@bot.tree.command(name="help", description="Show all available commands")
async def cmd_help(interaction: discord.Interaction):
    embed = discord.Embed(
        title     = "📖 EDGE — Command Reference",
        colour    = 0x5865F2,
        timestamp = datetime.now(timezone.utc),
    )
    embed.add_field(
        name  = "🔍 Market Analysis",
        value = (
            "`/scan` — live market scan for edge\n"
            "`/top` — top 3 highest-EV picks\n"
            "`/status` — last scan summary\n"
            "`/standings [sport]` — standings + championship odds\n"
            "`/tracking` — live game tracker (injury-triggered)"
        ),
        inline=False,
    )
    embed.add_field(
        name  = "📊 Paper Trading",
        value = (
            "`/mytrades` — open picks + settled P&L\n"
            "`/performance [days]` — your win rate + ROI\n"
            "Tap ✅ YES / ❌ NO on any alert to paper trade it"
        ),
        inline=False,
    )
    embed.add_field(
        name  = "👛 Trader Intel",
        value = (
            "`/traders` — top 10 smart money wallets\n"
            "`/wallet <address>` — vet a specific wallet\n"
            "`/watch <address>` — add wallet to watchlist\n"
            "`/unwatch <address>` — remove from watchlist\n"
            "`/watchlist` — view all watched wallets"
        ),
        inline=False,
    )
    embed.add_field(
        name  = "🌤️ Specialist Scanners",
        value = (
            "`/weatherscan` — weather markets vs Open-Meteo forecast\n"
            "`/cryptoscan` — crypto markets vs Binance lognormal model\n"
            "`/fedscan` — Fed/econ markets vs NY Fed + yield curve\n"
            "Auto-scan runs every 4h and alerts to this channel"
        ),
        inline=False,
    )
    embed.add_field(
        name  = "🚨 Insider Alerts",
        value = (
            "`/insider` — recent whale/insider bet alerts\n"
            "Alerts auto-fire when fresh wallets place large bets on niche markets"
        ),
        inline=False,
    )
    embed.add_field(
        name  = "🏥 Injuries",
        value = "`/injuries [sport]` — live injury report (nba, nfl, nhl, cfb, cbb, wnba, ncaaw)",
        inline=False,
    )
    embed.add_field(
        name  = "💬 AI Chat",
        value = "Just type anything in this channel — EDGE is always listening",
        inline=False,
    )
    embed.set_footer(text="Copy-trade + insider + specialist alerts auto-fire to your channel")
    await interaction.response.send_message(embed=embed)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "DISCORD_BOT_TOKEN not set in .env\n"
            "Create a bot at https://discord.com/developers/applications"
        )
    if not GUILD_ID:
        raise SystemExit(
            "DISCORD_GUILD_ID not set in .env\n"
            "Right-click your Discord server → Copy Server ID"
        )
    log.info(
        "Starting EDGE Discord bot (scan every %d min, injury every %d min)...",
        SCAN_INTERVAL_MIN, INJURY_REFRESH_MIN,
    )
    bot.run(BOT_TOKEN)


if __name__ == "__main__":
    main()
