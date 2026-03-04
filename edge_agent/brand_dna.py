"""Brand DNA — configuration layer for repurposing the prediction agent across domains.

Three dataclasses (StrategyDNA, CopyDNA, VisualDNA) form the DNA building blocks.
BrandDNA composes all three and exposes a combined system prompt for the briefing LLM.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class StrategyDNA:
    """Intelligence filter — defines what the agent watches and ignores.

    Injected into the gatekeeper AI agent that scores article relevance.
    """

    name: str
    core_topics: list[str]
    industry_keywords: list[str]
    ignore_topics: list[str]
    relevance_threshold: int          # 0-100; articles scoring below this are dropped
    market_themes: list[str]          # must align with MarketSnapshot.theme values

    def build_news_query(self) -> str:
        """Returns a NewsAPI-compatible OR-joined query from the top keywords."""
        return " OR ".join(self.industry_keywords[:8])

    def to_system_prompt(self) -> str:
        core = "\n".join(f"  - {t}" for t in self.core_topics)
        keywords = ", ".join(self.industry_keywords)
        ignore = "\n".join(f"  - {t}" for t in self.ignore_topics)
        return (
            f"STRATEGY DNA — {self.name.upper()} INTELLIGENCE FILTER\n\n"
            f"You are a gatekeeper AI agent. Your task is to analyze a news headline and return "
            f"a structured JSON object that scores its relevance to the following intelligence brief.\n\n"
            f"Core Topics (prioritize these):\n{core}\n\n"
            f"Industry Keywords: {keywords}\n\n"
            f"Ignore / Filter Out:\n{ignore}\n\n"
            f"Relevance Scoring Rules:\n"
            f"  - Score 80-100: Breaking news with a direct, imminent binary outcome.\n"
            f"  - Score 60-79: Developing story with probable market impact within 72 hours.\n"
            f"  - Score 40-59: Background context relevant to tracked themes.\n"
            f"  - Score 0-39: Tangential or irrelevant — this article should be excluded.\n\n"
            f"Minimum relevance threshold: {self.relevance_threshold} — articles scoring below "
            f"this threshold must be rejected from the pipeline.\n\n"
            f"Return ONLY a JSON object with exactly these fields:\n"
            f'{{"relevance": <int 0-100>, "quality": <float 0-1>, '
            f'"direction": <float -1 to 1, bearish to bullish>, '
            f'"confidence": <float 0-1>}}. '
            f"No extra keys, no markdown, no explanation."
        )


@dataclass
class CopyDNA:
    """Brand voice — defines how the agent communicates in its final output.

    Injected into the briefing LLM to enforce consistent tone and style.
    """

    persona: str
    tone: str
    style_rules: list[str]

    def to_system_prompt(self) -> str:
        rules = "\n".join(f"  - {r}" for r in self.style_rules)
        return (
            f"COPY DNA — BRAND VOICE\n\n"
            f"Persona: {self.persona}\n\n"
            f"Tone: {self.tone}\n\n"
            f"Style Rules:\n{rules}"
        )


@dataclass
class VisualDNA:
    """Aesthetic design — controls the structure and look of the final briefing.

    Injected into the briefing LLM alongside Copy DNA and Strategy DNA.
    """

    color_palette: dict[str, str]     # role → hex, e.g. {"primary": "#0A1628"}
    report_sections: list[str]        # ordered section names in the final output
    image_prompt_prefix: str          # prepended to header image generation prompts

    def to_system_prompt(self) -> str:
        palette = ", ".join(f"{role}={hex_}" for role, hex_ in self.color_palette.items())
        sections = " → ".join(self.report_sections)
        return (
            f"VISUAL DNA — REPORT DESIGN\n\n"
            f"Color Palette: {palette}\n\n"
            f"Report Structure (follow this exact section order): {sections}\n\n"
            f"Image Prompt Prefix (prepend to all header image prompts): "
            f'"{self.image_prompt_prefix}"'
        )


@dataclass
class BrandDNA:
    """Container that combines all three DNA blocks.

    Pass this to EdgeScanner and EdgeReporter to drive the full pipeline
    from data ingestion through final briefing generation.
    """

    strategy: StrategyDNA
    copy: CopyDNA
    visual: VisualDNA

    def to_briefing_prompt(self) -> str:
        """Full system prompt for the briefing LLM — combines all three blocks."""
        return (
            "You are the editor-in-chief of an autonomous market intelligence system. "
            "Synthesize the provided market recommendations, catalyst data, and watchlist "
            "into a structured strategic briefing. Follow all three DNA blocks below precisely.\n\n"
            f"{self.strategy.to_system_prompt()}\n\n"
            "---\n\n"
            f"{self.copy.to_system_prompt()}\n\n"
            "---\n\n"
            f"{self.visual.to_system_prompt()}\n\n"
            "---\n\n"
            "Return a JSON object with these top-level keys matching the report sections: "
            + str(self.visual.report_sections)
            + ". Each value is a string. Also include an 'image_prompt' key "
            "with a header image generation prompt built using the Visual DNA image prompt prefix."
        )
