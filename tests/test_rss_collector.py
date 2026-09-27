"""RSS collector failure reporting tests."""

from __future__ import annotations

from agent.collectors.rss_collector import RSSCollector


def fail_fetch(_url):
    raise OSError("offline")


def test_failed_feed_is_reported_after_retries(tmp_path, monkeypatch):
    config = tmp_path / "sources.yaml"
    config.write_text(
        "rss:\n  - name: Example\n    url: https://example.com/feed.xml\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "agent.collectors.rss_collector.feedparser.parse",
        fail_fetch,
    )
    monkeypatch.setattr("agent.collectors.rss_collector.time.sleep", lambda _: None)
    collector = RSSCollector(config)

    assert collector.collect() == []
    assert collector.failed_count == 1
