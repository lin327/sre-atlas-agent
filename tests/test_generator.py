"""Tests for agent.generator module."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock

import pytest
import yaml

from agent.collectors.rss_collector import CollectedItem, RSSCollector
from agent.generator import ContentGenerator, GeneratedPage, _slugify, validate_content
from agent.main import AtlasPipeline


@pytest.fixture
def sample_item():
    """Use the real RSS/GitHub contract, not the legacy conftest stub."""
    return CollectedItem(
        title="Understanding SLOs", url="https://example.com/slo-guide",
        source="Example feed", category="runbook", content="SLO reliability guide",
        tags=["sre", "slo"],
    )


def raw_page(*, body=None, **metadata):
    fields = {"title": "Test Page", **metadata}
    if body is None:
        body = "## 概述\n\n" + "这是一篇包含排障步骤与适用条件的中文 SRE 技术文档。" * 12
        body += "\n\n相关页面：[[network-stack|网络栈]]。\n"
    return "---\n" + yaml.safe_dump(fields, allow_unicode=True) + "---\n" + body


def frontmatter(page):
    return yaml.safe_load(page.content.split("---", 2)[1])


# ---------------------------------------------------------------------------
# validate_content
# ---------------------------------------------------------------------------
class TestValidateContent:
    """Content quality gate tests."""

    def test_validate_content_valid(self):
        raw = raw_page()
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
        mock_claude_client.messages.create.return_value.content[0].text = raw_page()
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


def test_collector_type_is_shared():
    from agent import generator
    from agent.collectors import github_collector

    assert generator.CollectedItem is CollectedItem is github_collector.CollectedItem


@pytest.mark.parametrize("category, expected", [
    ("architecture", "architectures"), ("runbook", "runbooks"),
    ("linux", "linux"), ("github", "docker"), ("", "docker"),
    ("../../escape", "docker"),
])
def test_category_comes_from_source_or_classifier(sample_item, category, expected):
    item = replace(sample_item, title="Docker storage guide", category=category)
    raw = raw_page(category="incidents").replace("## 概述", "## 完全不同的标题")
    page = ContentGenerator._parse_output(raw, item)
    assert page.category == frontmatter(page)["category"] == expected
    assert f"## 指定分类\n{expected}\n" in ContentGenerator._build_user_prompt(item)


@pytest.mark.parametrize("category, expected_type", [
    ("runbook", "runbook"), ("architecture", "architecture"),
    ("incidents", "incident"), ("comparisons", "comparison"), ("linux", "concept"),
])
def test_frontmatter_contract_overrides_model(sample_item, category, expected_type):
    raw = raw_page(title="Docker 网络指南", canonical=True, confidence="certain", type="unknown")
    page = ContentGenerator._parse_output(raw, replace(sample_item, category=category))
    metadata = frontmatter(page)
    assert metadata["canonical"] is False
    assert metadata["type"] == expected_type
    assert metadata["confidence"] == page.confidence == "low"
    assert metadata["title"] == "Docker 网络指南"
    assert page.content.split("---\n", 2)[2] == raw.split("---\n", 2)[2]


def test_valid_type_and_confidence_preserved(sample_item):
    page = ContentGenerator._parse_output(raw_page(type="fundamental", confidence="high"), sample_item)
    assert frontmatter(page)["type"] == "fundamental"
    assert page.confidence == "high"


def test_slug_discards_chinese_punctuation_and_issue_tokens():
    assert _slugify("Docker（网络）/镜像_issue_FLAKY_bugfix_(Guide)") == "docker-guide"
    assert _slugify("纯中文（知识页面）") == ""


@pytest.mark.parametrize("title", ["纯中文知识页面", "SLO", "a" * 61])
def test_invalid_slug_is_rejected(title):
    valid, issues = validate_content(raw_page(title=title))
    assert not valid and any("slug" in issue for issue in issues)


@pytest.mark.parametrize("title", ["abcd", "a" * 60, "Docker 网络指南"])
def test_valid_slug_boundaries(title):
    assert validate_content(raw_page(title=title)) == (True, [])


@pytest.mark.parametrize("title", [
    "[Bug] Grafana toaster broken", "fix: recover session", "Issue #1234",
    "etcd flaky test", "bugfixadd lock", "修复 Grafana 数据库错误", "TestRemoteWrite 测试不稳定",
    "  fix: retry connection", "[Fix] toaster regression",
])
def test_issue_titles_skipped_before_api(sample_item, mock_claude_client, title):
    generator = ContentGenerator(api_key="test-key")
    assert generator.generate_page(replace(sample_item, title=title)) is None
    mock_claude_client.messages.create.assert_not_called()


@pytest.mark.parametrize("raw", [
    raw_page(title="fix: toaster regression"), raw_page(title="中文标题"),
    raw_page(title=[]), "---\n- not a mapping\n---\n" + "content " * 40,
    raw_page(body="正文没有知识链接。" * 40),
    "---\ntitle: [broken YAML\n---\n" + "content " * 40,
])
def test_bad_generated_content_skipped(sample_item, mock_claude_client, raw):
    mock_claude_client.messages.create.return_value.content[0].text = raw
    assert ContentGenerator(api_key="test-key").generate_page(sample_item) is None


@pytest.mark.parametrize("fake_link", [
    "", "`[[network-stack]]`", "```md\n[[network-stack]]\n```", "<!-- [[network-stack]] -->",
])
def test_wikilink_must_be_in_body_prose(fake_link):
    raw = raw_page(description="[[network-stack]]", body="正文介绍运维实践。" * 40 + fake_link)
    valid, issues = validate_content(raw)
    assert not valid and "Body has no wikilink" in issues


def mock_collection(monkeypatch, items):
    monkeypatch.setattr(RSSCollector, "collect", lambda self: items)


@pytest.mark.parametrize("flag", [None, "false", "1", "TRUE", "true"])
def test_pipeline_inbox_default_and_explicit_publish(
    tmp_path, monkeypatch, sample_item, mock_claude_client, flag,
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PUBLISH_CANONICAL", raising=False)
    if flag is not None:
        monkeypatch.setenv("PUBLISH_CANONICAL", flag)
    mock_collection(monkeypatch, [sample_item])
    mock_claude_client.messages.create.return_value.content[0].text = raw_page(canonical=True)
    dedup = MagicMock()
    dedup.is_seen.return_value = False
    AtlasPipeline(config={"rss": [{}]}, dedup=dedup).run()
    expected = "output/runbooks/test-page.mdx" if flag == "true" else "output/inbox/runbooks/test-page.mdx"
    files = list(tmp_path.rglob("*.mdx"))
    assert files == [tmp_path / expected]
    assert yaml.safe_load(files[0].read_text().split("---", 2)[1])["canonical"] is False
    dedup.mark_seen.assert_called_once_with(
        url=sample_item.url, source=sample_item.source, category="runbooks", title="Test Page",
    )


@pytest.mark.parametrize("changes", [
    {"slug": "../../escape"}, {"slug": "x"}, {"slug": "issue-guide"},
    {"category": "../linux"}, {"content": raw_page(body="没有链接的正文。" * 40)},
])
def test_pipeline_rejects_invalid_outputs_without_marking_seen(
    tmp_path, monkeypatch, sample_item, mock_claude_client, changes,
):
    monkeypatch.delenv("PUBLISH_CANONICAL", raising=False)
    mock_collection(monkeypatch, [sample_item])
    page = replace(ContentGenerator._parse_output(raw_page(), sample_item), **changes)
    monkeypatch.setattr(ContentGenerator, "generate_batch", lambda self, items: [page])
    dedup = MagicMock()
    dedup.is_seen.return_value = False
    AtlasPipeline(config={"rss": [{}]}, output_dir=str(tmp_path), dedup=dedup).run()
    assert not list(tmp_path.rglob("*.mdx"))
    dedup.mark_seen.assert_not_called()


def test_slug_collision_preserves_first_draft_and_unwritten_source(
    tmp_path, monkeypatch, sample_item, mock_claude_client,
):
    monkeypatch.delenv("PUBLISH_CANONICAL", raising=False)
    second_item = replace(sample_item, url="https://example.com/second")
    mock_collection(monkeypatch, [sample_item, second_item])
    pages = [ContentGenerator._parse_output(raw_page(title=title), item) for title, item in [
        ("Docker 网络排障", sample_item), ("Docker 存储排障", second_item),
    ]]
    monkeypatch.setattr(ContentGenerator, "generate_batch", lambda self, items: pages)
    dedup = MagicMock()
    dedup.is_seen.return_value = False
    pipeline = AtlasPipeline(config={"rss": [{}]}, output_dir=str(tmp_path), dedup=dedup)
    pipeline.run()
    output = tmp_path / "inbox/runbooks/docker.mdx"
    assert output.read_text() == pages[0].content
    dedup.mark_seen.assert_called_once_with(
        url=sample_item.url, source=sample_item.source, category="runbooks", title=pages[0].title,
    )
    dedup.mark_seen.reset_mock()
    pipeline.run()
    assert output.read_text() == pages[0].content
    dedup.mark_seen.assert_not_called()


@pytest.mark.parametrize("suffix", ["src/pages", "src/pages/linux", "src/pages/inbox"])
def test_unreviewed_output_cannot_enter_public_routes(tmp_path, monkeypatch, suffix):
    monkeypatch.delenv("PUBLISH_CANONICAL", raising=False)
    with pytest.raises(ValueError, match="src/pages"):
        AtlasPipeline(config={}, output_dir=str(tmp_path / suffix))
    assert not list(tmp_path.iterdir())


def test_output_symlink_to_public_routes_is_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv("PUBLISH_CANONICAL", raising=False)
    published = tmp_path / "wiki/src/pages"
    published.mkdir(parents=True)
    alias = tmp_path / "output"
    alias.symlink_to(published, target_is_directory=True)
    with pytest.raises(ValueError, match="src/pages"):
        AtlasPipeline(config={}, output_dir=str(alias))
