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

async def _ai_reply(
    message: discord.Message,
    user_msg: str,
    user_id: int,
) -> str:
    """Build context and get an AI response, same logic as Telegram bot."""
    loop = asyncio.get_event_loop()

    mem       = _get_session(user_id)
    kb_ctx    = _kb.get_context_for_question(user_msg)
    sess_ctx  = mem.get_session_context(max_exchanges=4)
    prof_ctx  = _profiles.get_profile_context(user_id)

    # Recent scan opps
    svc   = _get_service()
    top   = svc.engine.top_opportunities(limit=3)
    scan_ctx = ""
    if top:
        lines = ["\nRecent scan opportunities:"]
        for r in top:
            lines.append(
                f"- {r.metadata.get('question', r.market_id)[:60]} "
                f"| market={r.market_prob:.0%} edge={r.edge:+.0%}"
            )
        scan_ctx = "\n".join(lines)

    system_prompt = (
        "You are EDGE, an AI prediction market analyst operating on Discord. "
        "Your job: help users find and act on mispriced prediction markets on "
        "Polymarket and Kalshi.\n\n"
        "PAPER TRADING — THIS IS A BUILT-IN FEATURE:\n"
        "• Every scan alert has ✅ YES / ❌ NO buttons — clicking logs a $10 virtual stake.\n"
        "• /mytrades — open picks + settled WIN/LOSS/VOID history + P&L.\n"
        "• /performance — your paper P&L, win rate, and ROI.\n\n"
        "PLATFORMS:\n"
        "• Polymarket — decentralized, USDC on Polygon, no KYC, 0% fees\n"
        "• Kalshi — US-regulated (CFTC), USD, KYC required, ~7% fee\n\n"
        "Be concise. Use Discord markdown (**bold**, *italic*, `code`). "
        "Keep replies under 300 words. Return plain text, not JSON.\n\n"
        "CRITICAL — YOU ARE A PREDICTION MARKET ANALYST, NOT A SPORTSBOOK:\n"
        "• Frame edges as probability, not spreads.\n"
        "• Prices are probabilities (0-100%). Positions are YES/NO contracts."
    )

    prompt = user_msg + kb_ctx + prof_ctx + sess_ctx + scan_ctx

    reply = await loop.run_in_executor(
        None,
        lambda: get_chat_response(prompt, task_type="creative", system_prompt=system_prompt),
    )
    reply = reply or "Sorry, I couldn't generate a response right now."

    # Save to session
    mem.add_exchange(user_msg, reply)
    _profiles.update_from_message(user_id, user_msg)

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

    loop = asyncio.get_event_loop()
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

    async def _handle(self, interaction: discord.Interaction, side: str):
        uid   = interaction.user.id
        rec   = self._rec
        prob  = rec.market_prob
        stake = 10.0
        ok    = _ot.record_user_pick(
            user_id      = uid,
            signal_id    = rec.market_id,
            market_id    = rec.market_id,
            side         = side,
            paper_stake  = stake,
            entry_prob   = prob,
        )
        if ok:
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
                fresh = await asyncio.get_event_loop().run_in_executor(
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
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _ot.resolve_pending)
    except Exception as exc:
        log.warning("resolution_job error: %s", exc)


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
            "• Vet smart money wallets for edges\n"
            "• Track injuries and their market impact\n"
            "• Help you build a paper trading track record\n\n"
            "**Quick start:**\n"
            "`/scan` — run a live market scan\n"
            "`/top` — top 3 highest-EV picks right now\n"
            "`/injuries nba` — live injury report\n"
            "`/mytrades` — your paper P&L\n\n"
            "Or just ask me anything — I'm always listening here."
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
        loop   = asyncio.get_event_loop()
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
        loop    = asyncio.get_event_loop()
        client  = _standings_mod.StandingsClient()
        text    = await loop.run_in_executor(None, lambda: client.format_standings(sport.lower()))
        if len(text) > 1900:
            text = text[:1900] + "\n…(truncated, use /standings for full table)"
        await interaction.followup.send(f"```\n{text}\n```")
    except Exception as exc:
        await interaction.followup.send(f"Error: {exc}")


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
            "`/standings [sport]` — standings + championship odds"
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
            "`/wallet <address>` — vet a specific wallet"
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
    embed.set_footer(text="Alerts fire automatically when markets move or injuries change")
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
