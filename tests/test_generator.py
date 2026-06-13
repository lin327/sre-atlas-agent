"""Tests for agent.generator module."""

from __future__ import annotations

from agent.generator import ContentGenerator, GeneratedPage, validate_content


# ---------------------------------------------------------------------------
# validate_content
# ---------------------------------------------------------------------------
class TestValidateContent:
    """Content quality gate tests."""

    def test_validate_content_valid(self):
        raw = (
            "---\n"
            "title: Test\n"
            "description: desc\n"
            "category: runbook\n"
            "---\n\n"
            "## Overview\n\n"
            "This is a valid page with enough content. " * 10
        )
        is_valid, issues = validate_content(raw)
        assert is_valid is True
        assert issues == []

    def test_validate_content_missing_frontmatter(self):
        raw = "No frontmatter here.\n\n" + "Some body content. " * 20
        is_valid, issues = validate_content(raw)
        assert is_valid is False
        assert any("frontmatter" in i.lower() for i in issues)

    def test_validate_content_too_short(self):
        raw = "---\ntitle: Short\n---\n\nTiny body."
        is_valid, issues = validate_content(raw)
        assert is_valid is False
        assert any("too short" in i.lower() for i in issues)

    def test_validate_content_placeholder(self):
        raw = (
            "---\ntitle: Placeholder\n---\n\n"
            "## Overview\n\n"
            "This page has a TODO marker and needs work. " * 10
        )
        is_valid, issues = validate_content(raw)
        assert is_valid is False
        assert any("placeholder" in i.lower() for i in issues)


# ---------------------------------------------------------------------------
# _parse_output (via ContentGenerator._parse_output static method)
# ---------------------------------------------------------------------------
class TestParseOutput:
    """Field extraction from generated markdown."""

    def test_parse_output_extracts_fields(self, sample_item):
        raw = (
            "---\n"
            "title: Parsed Title\n"
            "description: A parsed page\n"
            "category: kubernetes\n"
            "confidence: high\n"
            "---\n\n"
            "## Overview\n\n"
            "Content goes here. " * 10
        )
        page = ContentGenerator._parse_output(raw, sample_item)

        assert isinstance(page, GeneratedPage)
        assert page.title == "Parsed Title"
        assert page.confidence == "high"
        assert page.source_url == sample_item.url
        assert page.slug  # non-empty

    def test_parse_output_fallback_title(self, sample_item):
        raw = (
            "---\n"
            "description: No title field\n"
            "---\n\n"
            "## Overview\n\n"
            "Body content. " * 10
        )
        page = ContentGenerator._parse_output(raw, sample_item)
        assert page.title == sample_item.title

    def test_parse_output_default_confidence(self, sample_item):
        raw = (
            "---\n"
            "title: No Confidence\n"
            "---\n\n"
            "## Overview\n\n"
            "Body content. " * 10
        )
        page = ContentGenerator._parse_output(raw, sample_item)
        assert page.confidence == "medium"


# ---------------------------------------------------------------------------
# generate_page (with mocked Claude API)
# ---------------------------------------------------------------------------
class TestGeneratePage:
    """Integration test with mocked Anthropic client."""

    def test_generate_page_returns_page(self, sample_item, mock_claude_client):
        generator = ContentGenerator(api_key="test-key")
        page = generator.generate_page(sample_item)

        assert page is not None
        assert isinstance(page, GeneratedPage)
        assert page.source_url == sample_item.url

    def test_generate_page_returns_none_on_api_failure(self, sample_item, monkeypatch):
        from unittest.mock import MagicMock

        mock_client = MagicMock()
        mock_client.messages.create.side_effect = Exception("API down")
        monkeypatch.setattr(
            "anthropic.Anthropic", MagicMock(return_value=mock_client)
        )

        generator = ContentGenerator(api_key="test-key")
        page = generator.generate_page(sample_item)
        assert page is None
