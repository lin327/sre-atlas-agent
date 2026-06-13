"""Tests for agent.dedup module."""

from __future__ import annotations


class TestIsSeen:
    """is_seen() behaviour."""

    def test_is_seen_returns_false_for_new_url(self, dedup):
        assert dedup.is_seen("https://example.com/new") is False

    def test_mark_seen_and_is_seen(self, dedup):
        url = "https://example.com/article"
        dedup.mark_seen(url=url, source="rss", category="kubernetes", title="K8s Guide")
        assert dedup.is_seen(url) is True

    def test_mark_seen_duplicate_ignored(self, dedup):
        url = "https://example.com/dup"
        dedup.mark_seen(url=url, source="rss", category="linux", title="First")
        # Second insert with different metadata should not raise.
        dedup.mark_seen(url=url, source="github", category="docker", title="Second")
        # URL is still seen exactly once.
        assert dedup.is_seen(url) is True


class TestGetStats:
    """get_stats() aggregation."""

    def test_get_stats_empty(self, dedup):
        stats = dedup.get_stats()
        assert stats == {"by_source": {}, "by_category": {}}

    def test_get_stats_with_data(self, dedup):
        dedup.mark_seen("https://a.com", source="rss", category="kubernetes", title="A")
        dedup.mark_seen("https://b.com", source="rss", category="linux", title="B")
        dedup.mark_seen("https://c.com", source="github", category="kubernetes", title="C")

        stats = dedup.get_stats()
        assert stats["by_source"]["rss"] == 2
        assert stats["by_source"]["github"] == 1
        assert stats["by_category"]["kubernetes"] == 2
        assert stats["by_category"]["linux"] == 1

    def test_get_stats_null_category_grouped(self, dedup):
        """Items with NULL category should appear under 'uncategorised'."""
        dedup.mark_seen("https://x.com", source="rss", category="", title="X")
        stats = dedup.get_stats()
        assert "uncategorised" in stats["by_category"]
