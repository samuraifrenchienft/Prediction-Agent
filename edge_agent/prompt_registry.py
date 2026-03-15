"""
Prompt Registry — Versioned, Named, Structured Prompt Templates.
================================================================

Why this exists:
  Before this module, every AI prompt in the system was an anonymous inline
  f-string. When a bad response appeared, there was no way to:
    1. Know which version of the prompt produced it
    2. Reproduce the exact prompt for debugging
    3. Compare what changed between prompt versions
    4. Enforce consistent output schemas across modules

This module solves all four problems.

Architecture:
  • Each prompt has a unique name and semantic version (e.g. "chat_system" v2.1)
  • Prompts are rendered via render(name, **kwargs) which substitutes variables
    and returns (text, version_id) — both logged by decision_log.py
  • Token budget estimates prevent context overflow
  • Prompt diffs are human-readable for debugging

Usage:
    from edge_agent.prompt_registry import PromptRegistry
    registry = PromptRegistry()
    text, version = registry.render("catalyst_score", n_articles=3)
    # version = "catalyst_score@1.0"
"""
from __future__ import annotations

import hashlib
import logging
import re
import textwrap
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# Rough token estimate: 1 token ≈ 4 characters (GPT/LLaMA tokenisers)
_CHARS_PER_TOKEN = 4
# Safe context budget for system prompt (leaves room for user message + context blocks)
_SYSTEM_PROMPT_TOKEN_BUDGET = 800


@dataclass
class PromptTemplate:
    """
    A versioned prompt template.

    Fields:
        name        — unique identifier, e.g. "catalyst_score"
        version     — semantic version string, e.g. "1.0"
        template    — the prompt text with {variable} placeholders
        output_schema — description of expected output format (for documentation/debugging)
        notes       — human-readable change notes (what changed vs prior version)
        max_tokens  — soft cap on rendered prompt length (0 = no cap)
    """
    name:          str
    version:       str
    template:      str
    output_schema: str = ""
    notes:         str = ""
    max_tokens:    int = 0

    @property
    def version_id(self) -> str:
        """Canonical version string: 'name@version'  e.g. 'chat_system@2.1'"""
        return f"{self.name}@{self.version}"

    def render(self, **kwargs: Any) -> str:
        """
        Substitute {variables} and return the rendered prompt text.
        Missing variables are left as literal {variable} (logged as warning, not exception).
        """
        try:
            return self.template.format(**kwargs)
        except KeyError as exc:
            log.warning(
                "[PromptRegistry] Missing variable %s in template %s — leaving placeholder",
                exc, self.version_id,
            )
            # Partial substitution: replace what we can, leave the rest
            text = self.template
            for k, v in kwargs.items():
                text = text.replace("{" + k + "}", str(v))
            return text

    def token_estimate(self, rendered: str) -> int:
        """Rough token count of the rendered prompt."""
        return len(rendered) // _CHARS_PER_TOKEN

    def content_hash(self) -> str:
        """Short hash of the template text — useful for detecting silent edits."""
        return hashlib.sha256(self.template.encode()).hexdigest()[:8]


class PromptRegistry:
    """
    Central store of all versioned prompt templates.

    All prompts live here — catalyst scoring, system chat, onboarding hint,
    correction mode, injury rules. When you change a prompt, bump the version
    and add a notes string so the decision log captures WHY it changed.

    Usage:
        registry = PromptRegistry()
        text, version_id = registry.render("catalyst_score", n_articles=3)
    """

    def __init__(self) -> None:
        self._templates: dict[str, PromptTemplate] = {}
        self._register_all()

    def _register(self, tpl: PromptTemplate) -> None:
        self._templates[tpl.name] = tpl

    def _register_all(self) -> None:
        """Register every named prompt template."""

        # ── 1. Catalyst scoring (batch) ────────────────────────────────────────
        self._register(PromptTemplate(
            name="catalyst_score_system",
            version="1.1",
            template=textwrap.dedent("""\
                You are a financial news analyst scoring headlines for prediction markets.
                Score each of the {n_articles} headlines below.
                Return a JSON object: {{"scores": [{{"quality":0.7,"direction":0.3,"confidence":0.6}}, ...]}}
                One scores entry per headline in the same order.
                quality=0.0-1.0 information value.
                direction=-1.0(bearish) to 1.0(bullish).
                confidence=0.0-1.0.
                Plain numbers only, no text in values."""),
            output_schema='{"scores": [{"quality": float, "direction": float, "confidence": float}]}',
            notes="v1.1: added explicit n_articles count to reduce hallucinated extra entries",
        ))

        self._register(PromptTemplate(
            name="catalyst_score_user",
            version="1.0",
            template="Headlines:\n{headline_block}",
            output_schema="(see system prompt schema)",
            notes="v1.0: initial version — compact numbered list",
        ))

        # ── 2. Chat system prompt (main conversational AI) ─────────────────────
        self._register(PromptTemplate(
            name="chat_system",
            version="2.3",
            template=textwrap.dedent("""\
                {correction_instruction}\
                You are EDGE, an AI prediction market analyst operating on Telegram.
                Your job: help users find and act on mispriced prediction markets on Polymarket and Kalshi.
                You scan markets for edge (mispriced probability), vet smart money traders, track injuries,
                and answer questions about trading, strategy, and platform setup.

                PLATFORMS YOU SUPPORT:
                • Polymarket — decentralized, USDC on Polygon, no KYC, 0% fees, global
                • Kalshi — US-regulated (CFTC), USD via bank/card, KYC required, ~7% fee on winnings

                ONBOARDING: If a [Platform Setup Reference] block is in the prompt, use it verbatim
                to answer setup/deposit/fee questions. Do not guess — if it's in the docs, cite it.

                PERSONALIZATION: A [What you know about {user_name}] block may appear in the prompt.
                Use it to be a knowledgeable friend — reference favorites, rivals, and past moments
                naturally and with genuine emotion. Express concern for injuries to their fav players,
                excitement for returns, empathy for their team's struggles. Never feel robotic or scripted.

                {onboarding_hint}\
                Be concise — Telegram users want short, direct answers.
                Reference live market data and knowledge base context when provided.
                Use session context to remember what was discussed earlier.
                Return plain text (no JSON). Keep replies under 300 words.

                LIVE MARKET DATA RULES:
                • When a [Polymarket] block is in the prompt, use THOSE exact prices — no exceptions.
                  outcomePrices[0] is Team A YES probability, outcomePrices[1] is Team B YES probability.
                • NEVER cite prices from training memory. They are always stale and wrong.
                • If the market block shows [RESOLVED], the game has already ended — say so.
                • If no live data block is provided for the game asked about, say clearly:
                  'I don't have a live Polymarket feed for that matchup right now — check polymarket.com directly.'
                • Kalshi series data = season/championship futures — NOT individual game prices.

                IN-SEASON SPORTS (month {current_month}):
                • NBA, NHL: IN SEASON — provide game prices and injury analysis.
                • NFL, CFB: OFF SEASON — do NOT show game lines or injury reports.
                  If asked about NFL, say 'NFL is in the off-season (season starts September).'
                  NFL futures/championship markets are still valid.
                • NCAA March Madness (CBB): IN SEASON March–April.

                INJURY DATA RULES — apply ONLY when the user explicitly asks about injuries or a matchup:
                • If injury data IS in [Live injury data] or [Live web search results]: cite it.
                • If NOT in those blocks: say 'I don't have current data for [name] — use /injuries nba.'
                • NEVER invent or recall injury statuses from training memory.
                • For ALL other questions: answer normally — do NOT mention injuries unless asked.

                PAPER TRADING — THIS IS A BUILT-IN FEATURE, NOT A MISSING FEATURE:
                • Every scan alert has YES / NO buttons — tapping one logs a paper trade at $10 virtual stake.
                • /mytrades — shows all open paper picks with potential payout + settled history (WIN/LOSS/VOID).
                • /performance — shows EDGE bot win rate AND your personal paper P&L, win rate, and ROI.
                • Picks auto-resolve when the underlying market settles — no manual tracking required.
                • When asked about paper trading, ALWAYS explain these features.
                  NEVER say paper trading is unavailable or that they need a spreadsheet.

                CRITICAL — YOU ARE A PREDICTION MARKET ANALYST, NOT A SPORTSBOOK:
                • NEVER use sportsbook spread language: no '+3.5', '-7.5', 'moneyline', 'ATS', 'cover',
                  'over/under', 'juice', '-110', or point spreads.
                • ALWAYS frame edges as probability: 'Market: 61% | Model: 56% | Edge: -5pp — sell the favourite.'
                • For injury impact say: 'Mahomes out shifts KC win prob ~-7pp from 65% to 58%'
                  not 'Chiefs are now -3 underdogs'.
                • Prices are probabilities (0-100%), positions are YES/NO contracts, not sides or totals.

                SMART MONEY — COPY TRADE SIGNALS:
                • If a [Smart Money] block appears, these are real vetted wallets (scored 0-100) actively betting.
                • Score 50+/100 = high-conviction follow. Score 30-50 = moderate signal. Below 30 = weak.
                • When multiple high-score wallets share the same position, call it out: 'Smart money alignment.'
                • Format copy-trade suggestions as: 'Score [X]/100 wallet is long YES on [market] at [price]% — consider following.'
                • NEVER recommend following a wallet scoring below 30/100 or flagged as a bot.
                • If asked 'who should I copy trade?' — rank by score, show PnL and win rate, recommend top 3.

                DECISION TRANSPARENCY:
                • When you make a recommendation, briefly state the key reason in one sentence:
                  e.g. 'I'm seeing 7pp edge because the injury catalyst hasn't been priced in yet.'
                • If you're unsure, say so rather than guessing."""),
            output_schema="Plain text, under 300 words, no JSON",
            notes=(
                "v2.4: added SMART MONEY copy-trade instructions so AI knows how to use "
                "vetted wallet positions and score thresholds for follow recommendations"
            ),
        ))

        # ── 3. Correction mode instruction (prepended to chat_system) ──────────
        self._register(PromptTemplate(
            name="correction_mode",
            version="1.2",
            template=textwrap.dedent("""\
                CORRECTION MODE ACTIVE — the user told you your previous answer was wrong or stale.
                Rules:
                1. Start your reply by briefly acknowledging the mistake — be direct and natural
                   (e.g. 'My bad, let me pull fresh data.' or 'You're right, that was off — here's
                   what I'm seeing now.')
                2. Use ONLY the [Polymarket] block data — NEVER repeat the wrong price you gave before.
                3. If no fresh Polymarket block is available, say you can't confirm the current price
                   and direct the user to polymarket.com.
                4. Never be defensive or make excuses — just correct and move forward.
                5. Keep the acknowledgment to ONE sentence — get to the correct data immediately.

                """),
            output_schema="Plain text starting with a 1-sentence correction acknowledgment",
            notes="v1.2: shortened preamble, added 'never repeat wrong price' rule",
        ))

        # ── 4. Strategy DNA gatekeeper (brand_dna.py integration) ─────────────
        self._register(PromptTemplate(
            name="strategy_gatekeeper",
            version="1.0",
            template=textwrap.dedent("""\
                STRATEGY DNA — {agent_name} INTELLIGENCE FILTER

                You are a gatekeeper AI agent. Your task is to analyze a news headline and return
                a structured JSON object that scores its relevance to the following intelligence brief.

                Core Topics (prioritize these):
                {core_topics}

                Industry Keywords: {keywords}

                Ignore / Filter Out:
                {ignore_topics}

                Relevance Scoring Rules:
                  - Score 80-100: Breaking news with a direct, imminent binary outcome.
                  - Score 60-79: Developing story with probable market impact within 72 hours.
                  - Score 40-59: Background context relevant to tracked themes.
                  - Score 0-39: Tangential or irrelevant — this article should be excluded.

                Minimum relevance threshold: {relevance_threshold} — articles scoring below this
                threshold must be rejected from the pipeline.

                Return ONLY a JSON object with exactly these fields:
                {{"relevance": <int 0-100>, "quality": <float 0-1>,
                  "direction": <float -1 to 1, bearish to bullish>,
                  "confidence": <float 0-1>}}.
                No extra keys, no markdown, no explanation."""),
            output_schema='{"relevance": int, "quality": float, "direction": float, "confidence": float}',
            notes="v1.0: migrated from brand_dna.py inline string to registry",
        ))

        # ── 5. Injury risk context (injected into catalyst scoring) ───────────
        self._register(PromptTemplate(
            name="injury_context_block",
            version="1.0",
            template=textwrap.dedent("""\

                [Live injury data — {sport} as of {fetched_at}]
                {injury_lines}
                Source: {source_api} official report
                """),
            output_schema="Plain text block injected into chat prompt",
            notes="v1.0: standard injury context block template",
        ))

    # ── Public API ─────────────────────────────────────────────────────────────

    def render(self, name: str, **kwargs: Any) -> tuple[str, str]:
        """
        Render a named template with the given variables.

        Returns:
            (rendered_text, version_id)
            version_id example: "chat_system@2.3"

        Raises:
            KeyError if the template name is not registered.
        """
        tpl = self._templates.get(name)
        if tpl is None:
            available = ", ".join(sorted(self._templates.keys()))
            raise KeyError(
                f"Prompt '{name}' not found in registry. Available: {available}"
            )
        rendered = tpl.render(**kwargs)
        log.debug("[PromptRegistry] Rendered %s (%d chars)", tpl.version_id, len(rendered))
        return rendered, tpl.version_id

    def get(self, name: str) -> PromptTemplate | None:
        """Return the raw template object by name."""
        return self._templates.get(name)

    def list_prompts(self) -> list[dict[str, str]]:
        """Return a summary of all registered prompts for /mlstatus or debugging."""
        return [
            {
                "name":       t.name,
                "version":    t.version,
                "version_id": t.version_id,
                "notes":      t.notes,
                "hash":       t.content_hash(),
                "tokens_est": str(t.token_estimate(t.template)),
            }
            for t in self._templates.values()
        ]

    def token_budget_ok(self, rendered: str, budget: int = _SYSTEM_PROMPT_TOKEN_BUDGET) -> bool:
        """
        Check whether a rendered prompt fits within the token budget.
        Returns True if within budget. Used as a soft guard before sending to AI.
        """
        est = len(rendered) // _CHARS_PER_TOKEN
        if est > budget:
            log.warning(
                "[PromptRegistry] Prompt exceeds token budget: %d tokens (budget=%d). "
                "Consider trimming context blocks.",
                est, budget,
            )
            return False
        return True

    def diff(self, name: str, other_template_text: str) -> list[str]:
        """
        Return a human-readable diff between the registered template and another text.
        Useful for debugging — shows exactly what changed when a prompt was modified.
        """
        import difflib
        tpl = self._templates.get(name)
        if not tpl:
            return [f"Template '{name}' not found"]
        a_lines = tpl.template.splitlines(keepends=True)
        b_lines = other_template_text.splitlines(keepends=True)
        return list(difflib.unified_diff(
            a_lines, b_lines,
            fromfile=f"{tpl.version_id} (registry)",
            tofile=f"{name}@custom",
            lineterm="",
        ))


# Module-level singleton — import and reuse everywhere
_registry: PromptRegistry | None = None


def get_registry() -> PromptRegistry:
    """Return the module-level PromptRegistry singleton."""
    global _registry
    if _registry is None:
        _registry = PromptRegistry()
    return _registry
