"""Unit tests for Brand DNA dataclasses and presets."""
from edge_agent.brand_dna import BrandDNA, CopyDNA, StrategyDNA, VisualDNA
from edge_agent.presets import CRYPTO_DEFI_DNA, PREDICTION_MARKET_DNA


class TestStrategyDNA:
    def test_build_news_query_returns_nonempty_string(self):
        result = PREDICTION_MARKET_DNA.strategy.build_news_query()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_build_news_query_uses_first_eight_keywords(self):
        result = PREDICTION_MARKET_DNA.strategy.build_news_query()
        # Result should be OR-joined keywords
        assert " OR " in result
        parts = result.split(" OR ")
        assert len(parts) <= 8

    def test_build_news_query_contains_expected_keyword(self):
        result = PREDICTION_MARKET_DNA.strategy.build_news_query()
        # "federal reserve" is the first keyword in the preset
        assert "federal reserve" in result

    def test_to_system_prompt_contains_relevance_threshold(self):
        prompt = PREDICTION_MARKET_DNA.strategy.to_system_prompt()
        assert str(PREDICTION_MARKET_DNA.strategy.relevance_threshold) in prompt

    def test_to_system_prompt_contains_core_topics(self):
        prompt = PREDICTION_MARKET_DNA.strategy.to_system_prompt()
        # At least one core topic should appear verbatim
        assert "Monetary policy" in prompt

    def test_to_system_prompt_contains_ignore_section(self):
        prompt = PREDICTION_MARKET_DNA.strategy.to_system_prompt()
        assert "Ignore" in prompt or "Filter" in prompt

    def test_to_system_prompt_instructs_json_return(self):
        prompt = PREDICTION_MARKET_DNA.strategy.to_system_prompt()
        assert "JSON" in prompt
        assert "relevance" in prompt
        assert "quality" in prompt
        assert "direction" in prompt
        assert "confidence" in prompt

    def test_relevance_threshold_is_set(self):
        assert PREDICTION_MARKET_DNA.strategy.relevance_threshold == 60

    def test_market_themes_nonempty(self):
        assert len(PREDICTION_MARKET_DNA.strategy.market_themes) > 0


class TestCopyDNA:
    def test_to_system_prompt_contains_persona(self):
        prompt = PREDICTION_MARKET_DNA.copy.to_system_prompt()
        assert "COPY DNA" in prompt
        assert PREDICTION_MARKET_DNA.copy.persona[:30] in prompt

    def test_to_system_prompt_contains_tone(self):
        prompt = PREDICTION_MARKET_DNA.copy.to_system_prompt()
        assert "Tone" in prompt
        assert "Professional" in prompt

    def test_to_system_prompt_lists_style_rules(self):
        prompt = PREDICTION_MARKET_DNA.copy.to_system_prompt()
        assert "Style Rules" in prompt
        # Every style rule should appear in the rendered prompt
        for rule in PREDICTION_MARKET_DNA.copy.style_rules:
            assert rule in prompt


class TestVisualDNA:
    def test_to_system_prompt_contains_color_palette(self):
        prompt = PREDICTION_MARKET_DNA.visual.to_system_prompt()
        for role, hex_ in PREDICTION_MARKET_DNA.visual.color_palette.items():
            assert role in prompt
            assert hex_ in prompt

    def test_to_system_prompt_contains_report_sections(self):
        prompt = PREDICTION_MARKET_DNA.visual.to_system_prompt()
        for section in PREDICTION_MARKET_DNA.visual.report_sections:
            assert section in prompt

    def test_to_system_prompt_contains_image_prompt_prefix(self):
        prompt = PREDICTION_MARKET_DNA.visual.to_system_prompt()
        assert PREDICTION_MARKET_DNA.visual.image_prompt_prefix[:20] in prompt

    def test_report_sections_ordered_correctly(self):
        sections = PREDICTION_MARKET_DNA.visual.report_sections
        assert sections[0] == "Header"
        assert sections[-1] == "Footer"


class TestBrandDNA:
    def test_to_briefing_prompt_combines_all_blocks(self):
        prompt = PREDICTION_MARKET_DNA.to_briefing_prompt()
        # All three DNA blocks should be present
        assert "STRATEGY DNA" in prompt
        assert "COPY DNA" in prompt
        assert "VISUAL DNA" in prompt

    def test_to_briefing_prompt_references_report_sections(self):
        prompt = PREDICTION_MARKET_DNA.to_briefing_prompt()
        # The section list should appear in the JSON structure instruction
        for section in PREDICTION_MARKET_DNA.visual.report_sections:
            assert section in prompt

    def test_to_briefing_prompt_includes_image_prompt_instruction(self):
        prompt = PREDICTION_MARKET_DNA.to_briefing_prompt()
        assert "image_prompt" in prompt


class TestCryptoDeFiPreset:
    def test_preset_exists_and_is_brand_dna(self):
        assert isinstance(CRYPTO_DEFI_DNA, BrandDNA)

    def test_different_threshold_from_prediction_market(self):
        # Crypto preset has a different (higher) threshold to tighten signal quality
        assert CRYPTO_DEFI_DNA.strategy.relevance_threshold != PREDICTION_MARKET_DNA.strategy.relevance_threshold

    def test_different_query_from_prediction_market(self):
        crypto_query = CRYPTO_DEFI_DNA.strategy.build_news_query()
        pm_query = PREDICTION_MARKET_DNA.strategy.build_news_query()
        assert crypto_query != pm_query

    def test_crypto_themes_present(self):
        assert "defi" in CRYPTO_DEFI_DNA.strategy.market_themes or \
               "crypto" in CRYPTO_DEFI_DNA.strategy.market_themes

    def test_different_persona(self):
        assert CRYPTO_DEFI_DNA.copy.persona != PREDICTION_MARKET_DNA.copy.persona

    def test_different_color_palette(self):
        assert CRYPTO_DEFI_DNA.visual.color_palette != PREDICTION_MARKET_DNA.visual.color_palette
