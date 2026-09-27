"""Tests for agent.generator module."""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from datetime import UTC, date, datetime
from unittest.mock import MagicMock

import pytest
import yaml

from agent.collectors.rss_collector import CollectedItem, RSSCollector
from agent.content_schema import SCHEMA, validate_frontmatter
from agent.dedup import Dedup
from agent.generator import ContentGenerator, GeneratedPage, _slugify, validate_content
from agent.main import AtlasPipeline, main, select_items_for_generation
from config.settings import MAX_INPUT_CHARS


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

    def test_parse_output_missing_title_fails(self, sample_item):
        raw = (
            "---\n"
            "description: No title field\n"
            "---\n\n"
            "## Overview\n\n"
            "Body content. " * 10
        )
        with pytest.raises(ValueError, match="title"):
            ContentGenerator._parse_output(raw, sample_item)

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


def test_model_metadata_normalized_to_shared_schema(sample_item):
    raw = raw_page(
        title="  Docker 网络指南  ", canonical=True, category="untrusted",
        created=date(2026, 1, 1), lastUpdated=date(2026, 1, 2),
        sources=[
            {"url": sample_item.url, "title": "Model changed the source"},
            "https://example.com/secondary",
            {"url": "https://example.com/third", "title": " 第三来源 ", "unknown": True},
            {"url": "https://example.com/secondary", "title": "Duplicate"},
            {"url": "javascript:alert(1)", "title": "Invalid"},
            "ftp://example.com/file", "https://invalid host/path", "https://example.com:bad", {}, None,
        ],
        tags=[" docker ", None, 7, "", " ", "docker", "排障"],
        order=99, unknown="ignored", slug="model-suggested-slug",
    )
    page = ContentGenerator._parse_output(raw, sample_item)
    metadata = frontmatter(page)
    validate_frontmatter(metadata, page.slug)

    assert metadata["title"] == "Docker 网络指南"
    assert metadata["category"] == "runbooks"
    assert metadata["canonical"] is False
    assert metadata["created"] == "2026-01-01"
    assert metadata["updated"] == "2026-01-02"
    assert metadata["sources"] == [
        {"url": sample_item.url, "title": sample_item.title},
        {"url": "https://example.com/secondary", "title": "https://example.com/secondary"},
        {"url": "https://example.com/third", "title": "第三来源"},
    ]
    assert metadata["tags"] == ["docker", "排障"]
    assert set(metadata) == set(SCHEMA["required"])
    assert page.slug == "docker"
    assert page.content.split("---\n", 2)[2] == raw.split("---\n", 2)[2]


@pytest.mark.parametrize("updated", [None, "2026-02-30", "20260102", 123, []])
def test_invalid_dates_and_collections_use_safe_defaults(sample_item, updated):
    before = datetime.now(UTC).date().isoformat()
    page = ContentGenerator._parse_output(
        raw_page(created="not a date", updated=updated, tags="wrong", sources="wrong"),
        sample_item,
    )
    after = datetime.now(UTC).date().isoformat()
    metadata = frontmatter(page)
    validate_frontmatter(metadata, page.slug)
    assert metadata["created"] == metadata["updated"]
    assert metadata["updated"] in {before, after}
    assert metadata["tags"] == sample_item.tags
    assert metadata["sources"] == [{"url": sample_item.url, "title": sample_item.title}]


def test_updated_takes_precedence_and_created_defaults_to_updated(sample_item):
    page = ContentGenerator._parse_output(
        raw_page(updated="2026-01-03", lastUpdated="2026-01-02"), sample_item,
    )
    assert frontmatter(page)["created"] == frontmatter(page)["updated"] == "2026-01-03"


@pytest.mark.parametrize("changes", [
    {"title": ""}, {"created": "2026-02-30"}, {"canonical": "false"},
    {"category": "runbook"}, {"tags": ["valid", 1]}, {"extra": True},
    {"sources": [{"url": "ftp://example.com/file", "title": "Invalid"}]},
    {"sources": [{"url": "https://example.com:bad", "title": "Invalid"}]},
    {"sources": [{"url": "https:example.com", "title": "Invalid"}]},
    {"sources": [{"url": "https://example.com", "title": ""}]},
    {"sources": [{"url": "https://example.com"}]},
    {"sources": [{"url": "https://example.com", "title": "Valid", "extra": True}]},
])
def test_shared_schema_rejects_invalid_metadata(sample_item, changes):
    page = ContentGenerator._parse_output(raw_page(), sample_item)
    with pytest.raises(ValueError):
        validate_frontmatter({**frontmatter(page), **changes}, page.slug)


def test_shared_schema_rejects_missing_title_and_illegal_slug(sample_item):
    page = ContentGenerator._parse_output(raw_page(), sample_item)
    metadata = frontmatter(page)
    del metadata["title"]
    with pytest.raises(ValueError, match="required"):
        validate_frontmatter(metadata, page.slug)
    with pytest.raises(ValueError, match="slug"):
        validate_frontmatter(frontmatter(page), "../escape")


def test_invalid_source_url_is_skipped_after_normalization(sample_item, mock_claude_client):
    mock_claude_client.messages.create.return_value.content[0].text = raw_page()
    assert ContentGenerator(api_key="test-key").generate_page(
        replace(sample_item, url="javascript:alert(1)"),
    ) is None


@pytest.mark.parametrize("slug", [None, [], "", "X/invalid", "../escape", "issue-guide"])
def test_invalid_model_slug_is_rejected(sample_item, mock_claude_client, slug):
    raw = raw_page(slug=slug)
    valid, issues = validate_content(raw)
    assert not valid and any("slug" in issue for issue in issues)
    mock_claude_client.messages.create.return_value.content[0].text = raw
    assert ContentGenerator(api_key="test-key").generate_page(sample_item) is None


def test_slug_discards_chinese_punctuation_and_issue_tokens():
    assert _slugify("Docker（网络）/镜像_issue_FLAKY_bugfix_(Guide)") == "docker-guide"
    assert re.fullmatch(r"topic-[0-9a-f]{8}", _slugify("纯中文（知识页面）"))


@pytest.mark.parametrize("slug", ["", "x", "a" * 61, "../escape", "issue-guide"])
def test_invalid_explicit_slug_is_rejected(slug):
    valid, issues = validate_content(raw_page(), slug=slug)
    assert not valid and any("slug" in issue for issue in issues)


@pytest.mark.parametrize("title", [
    "abcd", "a" * 60, "Docker 网络指南", "纯中文知识页面", "SLO", "a" * 61,
])
def test_title_produces_stable_valid_slug(title, sample_item):
    slug = _slugify(title, sample_item.url)
    assert 4 <= len(slug) <= 60
    assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug)
    assert _slugify(title, sample_item.url) == _slugify(slug) == slug
    assert validate_content(raw_page(title=title), source_url=sample_item.url) == (True, [])
    assert validate_content(raw_page(title=title), slug=slug) == (True, [])


def test_chinese_slug_distinguishes_sources_and_titles():
    first = _slugify("纯中文知识页面", "https://example.com/one")
    assert first != _slugify("纯中文知识页面", "https://example.com/two")
    assert first != _slugify("另一篇中文知识页面", "https://example.com/one")


def test_chinese_title_is_generated_and_written(
    tmp_path, monkeypatch, sample_item, mock_claude_client,
):
    monkeypatch.delenv("PUBLISH_CANONICAL", raising=False)
    item = replace(sample_item, title="纯中文知识页面")
    mock_collection(monkeypatch, [item])
    mock_claude_client.messages.create.return_value.content[0].text = raw_page(title=item.title)
    dedup = MagicMock()
    dedup.is_seen.return_value = False

    AtlasPipeline(config={"rss": [{}]}, output_dir=str(tmp_path), dedup=dedup).run()

    files = list(tmp_path.rglob("*.mdx"))
    assert len(files) == 1
    assert re.fullmatch(r"topic-[0-9a-f]{8}", files[0].stem)
    assert item.title in files[0].read_text()
    dedup.mark_seen.assert_called_once_with(
        url=item.url, source=item.source, category="runbooks", title=item.title,
    )


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
    raw_page(title="fix: toaster regression"), raw_page(title=""),
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


def test_pipeline_logs_summary_when_no_items(monkeypatch, caplog, tmp_path):
    caplog.set_level(logging.INFO)
    mock_collection(monkeypatch, [])

    result = AtlasPipeline(
        config={"rss": [{}]}, output_dir=str(tmp_path), dry_run=True,
    ).run()

    assert result.status == "no_updates"
    assert (
        "pipeline_summary collected=0 selected=0 generated=0 reused=0 written=0 "
        "skipped=0 failed=0 budget_hit=0 status=no_updates"
    ) in caplog.text


def test_generation_selection_normalizes_and_deduplicates_before_limits(sample_item, caplog):
    caplog.set_level(logging.INFO)
    items = [
        replace(sample_item, source="rss-a", url="HTTPS://Example.com:443/one#section"),
        replace(sample_item, source="rss-a", url="https://example.com/one"),
        replace(sample_item, source="rss-a", url="https://example.com/two"),
        replace(sample_item, source="rss-a", url="https://example.com/three"),
        replace(sample_item, source="rss-b", url="https://example.com/four"),
        replace(sample_item, source="rss-b", url="https://example.com/five"),
    ]

    selection = select_items_for_generation(
        items, max_items_per_source=2, max_pages_per_run=3,
    )

    assert [item.url for item in selection.items] == [
        "https://example.com/one", "https://example.com/two", "https://example.com/four",
    ]
    assert selection.budget_hit == 2
    assert "Skipping duplicate normalized URL" in caplog.text
    assert "MAX_ITEMS_PER_SOURCE=2" in caplog.text
    assert "MAX_PAGES_PER_RUN=3" in caplog.text


def test_generation_selection_skips_oversized_items_before_claude(
    sample_item, caplog, mock_claude_client, tmp_path, monkeypatch,
):
    caplog.set_level(logging.INFO)
    monkeypatch.delenv("PUBLISH_CANONICAL", raising=False)
    oversized = replace(
        sample_item,
        url="https://example.com/oversized",
        content="x" * (MAX_INPUT_CHARS + 1),
    )
    mock_collection(monkeypatch, [oversized, sample_item])
    mock_claude_client.messages.create.return_value.content[0].text = raw_page()
    dedup = MagicMock()
    dedup.is_seen.return_value = False

    result = AtlasPipeline(config={"rss": [{}]}, output_dir=str(tmp_path), dedup=dedup).run()

    mock_claude_client.messages.create.assert_called_once()
    assert len(list(tmp_path.rglob("*.mdx"))) == 1
    assert result.budget_hit == 1
    assert "Skipping oversized source item" in caplog.text
    assert (
        "pipeline_summary collected=2 selected=1 generated=1 reused=0 written=1 "
        "skipped=0 failed=0 budget_hit=1"
    ) in caplog.text


def test_generation_selection_uses_normalized_url_for_persistent_dedup(sample_item):
    dedup = MagicMock()
    dedup.is_seen.side_effect = lambda url: url == "https://example.com/old"
    items = [
        replace(sample_item, url="HTTPS://Example.com:443/old#section"),
        replace(sample_item, url="https://example.com/new"),
    ]

    selection = select_items_for_generation(items, dedup=dedup)

    assert [item.url for item in selection.items] == ["https://example.com/new"]
    assert selection.budget_hit == 0
    assert [call.args[0] for call in dedup.is_seen.call_args_list] == [
        "https://example.com/old", "https://example.com/new",
    ]


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
    monkeypatch.setattr(ContentGenerator, "generate_page", lambda self, item: page)
    dedup = MagicMock()
    dedup.is_seen.return_value = False
    AtlasPipeline(config={"rss": [{}]}, output_dir=str(tmp_path), dedup=dedup).run()
    assert not list(tmp_path.rglob("*.mdx"))
    dedup.mark_seen.assert_not_called()


def test_slug_collision_keeps_both_drafts_and_marks_both_sources(
    tmp_path, monkeypatch, sample_item, mock_claude_client, dedup,
):
    monkeypatch.delenv("PUBLISH_CANONICAL", raising=False)
    second_item = replace(sample_item, url="https://example.com/second")
    mock_collection(monkeypatch, [sample_item, second_item])
    pages = [ContentGenerator._parse_output(raw_page(title=title), item) for title, item in [
        ("Docker 网络排障", sample_item), ("Docker 存储排障", second_item),
    ]]
    generated_pages = iter(pages)
    monkeypatch.setattr(ContentGenerator, "generate_page", lambda self, item: next(generated_pages))
    pipeline = AtlasPipeline(config={"rss": [{}]}, output_dir=str(tmp_path), dedup=dedup)
    pipeline.run()
    output = tmp_path / "inbox/runbooks/docker.mdx"
    assert output.read_text() == pages[0].content
    assert output.with_name("docker-2.mdx").read_text() == pages[1].content
    assert dedup.is_seen(sample_item.url)
    assert dedup.is_seen(second_item.url)
    pipeline.run()
    assert output.read_text() == pages[0].content
    assert len(list(tmp_path.rglob("*.mdx"))) == 2


@pytest.mark.parametrize("title", ["Docker 网络排障", "a" * 60])
def test_slug_collision_with_existing_files_preserves_length_limit(
    tmp_path, monkeypatch, sample_item, mock_claude_client, title,
):
    monkeypatch.delenv("PUBLISH_CANONICAL", raising=False)
    mock_collection(monkeypatch, [sample_item])
    page = ContentGenerator._parse_output(raw_page(title=title), sample_item)
    monkeypatch.setattr(ContentGenerator, "generate_page", lambda self, item: page)
    folder = tmp_path / "inbox/runbooks"
    folder.mkdir(parents=True)
    original = folder / f"{page.slug}.mdx"
    second = folder / f"{page.slug[:58].rstrip('-')}-2.mdx"
    original.write_text("First draft")
    second.write_text("Second draft")
    dedup = MagicMock()
    dedup.is_seen.return_value = False

    AtlasPipeline(config={"rss": [{}]}, output_dir=str(tmp_path), dedup=dedup).run()

    assert original.read_text() == "First draft"
    assert second.read_text() == "Second draft"
    third = folder / f"{page.slug[:58].rstrip('-')}-3.mdx"
    assert third.read_text() == page.content
    assert validate_content(third.read_text(), slug=third.stem) == (True, [])
    dedup.mark_seen.assert_called_once()


def test_mark_seen_failure_is_nonzero_and_reuses_persisted_page_on_retry(
    tmp_path, monkeypatch, sample_item, mock_claude_client,
):
    monkeypatch.delenv("PUBLISH_CANONICAL", raising=False)
    mock_collection(monkeypatch, [sample_item])
    mock_claude_client.messages.create.return_value.content[0].text = raw_page()
    dedup = Dedup(db_path=str(tmp_path / "dedup.db"))
    original_mark_seen = dedup.mark_seen
    mark_seen_calls = 0

    def fail_once(**kwargs):
        nonlocal mark_seen_calls
        mark_seen_calls += 1
        if mark_seen_calls == 1:
            raise OSError("temporary database write failure")
        original_mark_seen(**kwargs)

    monkeypatch.setattr(dedup, "mark_seen", fail_once)
    pipeline = AtlasPipeline(
        config={"rss": [{}]}, output_dir=str(tmp_path / "output"), dedup=dedup,
    )

    first = pipeline.run()
    assert first.status == "failed" and first.failed == 1 and first.written == 1
    assert dedup.get_generated_result(sample_item.url) is not None
    assert len(list((tmp_path / "output").rglob("*.mdx"))) == 1

    second = pipeline.run()
    assert second.status == "written" and second.reused == 1 and second.written == 1
    assert mock_claude_client.messages.create.call_count == 1
    assert dedup.is_seen(sample_item.url)
    assert dedup.get_generated_result(sample_item.url) is None
    assert len(list((tmp_path / "output").rglob("*.mdx"))) == 1


def test_write_failure_reuses_persisted_result_without_second_api_call(
    tmp_path, monkeypatch, sample_item, mock_claude_client,
):
    monkeypatch.delenv("PUBLISH_CANONICAL", raising=False)
    mock_collection(monkeypatch, [sample_item])
    mock_claude_client.messages.create.return_value.content[0].text = raw_page()
    dedup = Dedup(db_path=str(tmp_path / "dedup.db"))
    pipeline = AtlasPipeline(
        config={"rss": [{}]}, output_dir=str(tmp_path / "output"), dedup=dedup,
    )
    from agent.main import _write_page_atomically

    def fail_write(output_dir, page):
        raise OSError("temporary disk write failure")

    monkeypatch.setattr("agent.main._write_page_atomically", fail_write)
    first = pipeline.run()
    assert first.status == "failed" and first.written == 0
    assert dedup.get_generated_result(sample_item.url) is not None
    mock_claude_client.messages.create.assert_called_once()

    monkeypatch.setattr("agent.main._write_page_atomically", _write_page_atomically)
    second = pipeline.run()
    assert second.status == "written" and second.reused == 1
    assert mock_claude_client.messages.create.call_count == 1
    assert dedup.is_seen(sample_item.url)
    assert len(list((tmp_path / "output").rglob("*.mdx"))) == 1


def test_once_returns_nonzero_and_exports_partial_failure(
    tmp_path, monkeypatch, sample_item, mock_claude_client,
):
    monkeypatch.delenv("PUBLISH_CANONICAL", raising=False)
    config = tmp_path / "sources.yaml"
    config.write_text("rss: [{}]\n")
    mock_collection(monkeypatch, [sample_item])
    mock_claude_client.messages.create.return_value.content[0].text = raw_page()
    dedup = MagicMock()
    dedup.is_seen.return_value = False
    dedup.mark_seen.side_effect = OSError("temporary database write failure")
    monkeypatch.setattr("agent.main.Dedup", lambda: dedup)
    github_output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))

    code = main([
        "--once", "--config", str(config), "--output", str(tmp_path / "output"),
    ])

    assert code == 1
    assert "result=failed" in github_output.read_text()
    assert "has_written=true" in github_output.read_text()
    assert "failed=1" in github_output.read_text()


def test_once_reports_no_updates_as_success(tmp_path, monkeypatch):
    config = tmp_path / "sources.yaml"
    config.write_text("rss: [{}]\n")
    mock_collection(monkeypatch, [])
    monkeypatch.setattr("agent.main.Dedup", MagicMock())
    github_output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))

    assert main([
        "--once", "--config", str(config), "--output", str(tmp_path / "output"),
    ]) == 0
    assert "result=no_updates" in github_output.read_text()
    assert "has_written=false" in github_output.read_text()
    assert "failed=0" in github_output.read_text()


def test_once_reports_collector_failure_as_nonzero(tmp_path, monkeypatch):
    config = tmp_path / "sources.yaml"
    config.write_text("rss: [{}]\n")

    def fail_collect(collector):
        collector._failed_count = 1
        return []

    monkeypatch.setattr(RSSCollector, "collect", fail_collect)
    monkeypatch.setattr("agent.main.Dedup", MagicMock())
    github_output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))

    assert main([
        "--once", "--config", str(config), "--output", str(tmp_path / "output"),
    ]) == 1
    assert "result=failed" in github_output.read_text()
    assert "has_written=false" in github_output.read_text()
    assert "failed=1" in github_output.read_text()


def test_dry_run_collects_and_classifies_without_api_or_writes(
    tmp_path, monkeypatch, sample_item, mock_claude_client, caplog,
):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config = tmp_path / "sources.yaml"
    config.write_text("rss: [{}]\n")
    output = tmp_path / "output"
    items = [
        replace(sample_item, title="中文架构指南", category="architecture"),
        replace(sample_item, title="Docker 网络排障", category="", url="https://example.com/docker"),
    ]
    collect = MagicMock(return_value=items)
    monkeypatch.setattr(RSSCollector, "collect", collect)
    create_db = MagicMock()
    monkeypatch.setattr("agent.main.Dedup", create_db)
    create_client = MagicMock(return_value=mock_claude_client)
    monkeypatch.setattr("anthropic.Anthropic", create_client)

    with caplog.at_level(logging.INFO):
        for _ in range(2):
            assert main(["--once", "--dry-run", "--config", str(config), "--output", str(output)]) == 0

    assert collect.call_count == 2
    assert "[architectures] 中文架构指南" in caplog.text
    assert "[docker] Docker 网络排障" in caplog.text
    assert items[0].url in caplog.text
    create_client.assert_not_called()
    mock_claude_client.messages.create.assert_not_called()
    create_db.assert_not_called()
    assert not output.exists()


def test_generate_anyway_explicitly_pays_and_writes_drafts_without_db(
    tmp_path, monkeypatch, sample_item, mock_claude_client, caplog,
):
    monkeypatch.delenv("PUBLISH_CANONICAL", raising=False)
    mock_collection(monkeypatch, [sample_item])
    mock_claude_client.messages.create.return_value.content[0].text = raw_page()
    create_db = MagicMock()
    monkeypatch.setattr("agent.main.Dedup", create_db)
    config = tmp_path / "sources.yaml"
    config.write_text("rss: [{}]\n")
    output = tmp_path / "output"

    assert main([
        "--once", "--dry-run", "--generate-anyway", "--config", str(config), "--output", str(output),
    ]) == 0

    mock_claude_client.messages.create.assert_called_once()
    assert (output / "inbox/runbooks/test-page.mdx").is_file()
    assert "付费 Claude API" in caplog.text
    create_db.assert_not_called()


def test_generate_anyway_requires_dry_run():
    with pytest.raises(SystemExit) as error:
        main(["--generate-anyway"])
    assert error.value.code == 2
    with pytest.raises(ValueError, match="requires --dry-run"):
        AtlasPipeline(config={}, generate_anyway=True)


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
