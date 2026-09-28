"""RSS collector timeout and response-size guard tests."""

from __future__ import annotations

import requests

from agent.collectors import rss_collector
from agent.collectors.rss_collector import RSSCollector


class FakeResponse:
    def __init__(self, chunks: list[bytes], headers: dict[str, str] | None = None):
        self.chunks = chunks
        self.headers = headers or {}
        self.closed = False
        self.iterated = False

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int):
        self.iterated = True
        assert chunk_size == rss_collector._STREAM_CHUNK_BYTES
        yield from self.chunks

    def close(self) -> None:
        self.closed = True


def make_collector(tmp_path, monkeypatch, *, category: str = "linux") -> RSSCollector:
    config = tmp_path / "sources.yaml"
    config.write_text(
        f"rss:\n  - name: Example\n    url: https://example.com/feed.xml\n    category: {category}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("agent.collectors.rss_collector.time.sleep", lambda _: None)
    return RSSCollector(config)


def test_feed_download_uses_connect_and_read_timeout(tmp_path, monkeypatch):
    response = FakeResponse(
        [
            (
                b"<?xml version='1.0'?><rss version='2.0'><channel><title>Example</title>"
                b"<item><title>Linux guide</title><link>https://example.com/linux</link>"
                b"<description>Kernel guide</description></item></channel></rss>"
            )
        ],
    )
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return response

    monkeypatch.setattr("agent.collectors.rss_collector.requests.get", get)
    collector = make_collector(tmp_path, monkeypatch)

    items = collector.collect()

    assert calls[0][1]["timeout"] == (5, 15)
    assert calls[0][1]["stream"] is True
    assert items[0].title == "Linux guide"
    assert items[0].category == "linux"
    assert response.closed


def test_request_timeout_is_retried_then_reported(tmp_path, monkeypatch):
    calls = 0

    def timeout(_url, **_kwargs):
        nonlocal calls
        calls += 1
        raise requests.Timeout("read timed out")

    monkeypatch.setattr("agent.collectors.rss_collector.requests.get", timeout)
    collector = make_collector(tmp_path, monkeypatch)

    assert collector.collect() == []
    assert calls == rss_collector._MAX_RETRIES
    assert collector.failed_count == 1


def test_declared_oversized_response_is_rejected_before_streaming(
    tmp_path, monkeypatch
):
    response = FakeResponse(
        [], {"Content-Length": str(rss_collector._MAX_FEED_BYTES + 1)}
    )
    monkeypatch.setattr("agent.collectors.rss_collector._MAX_RETRIES", 1)
    monkeypatch.setattr(
        "agent.collectors.rss_collector.requests.get",
        lambda *_args, **_kwargs: response,
    )
    collector = make_collector(tmp_path, monkeypatch)

    assert collector.collect() == []
    assert not response.iterated
    assert response.closed
    assert collector.failed_count == 1


def test_streamed_response_is_stopped_at_byte_limit(tmp_path, monkeypatch):
    response = FakeResponse([b"123", b"45"])
    monkeypatch.setattr("agent.collectors.rss_collector._MAX_FEED_BYTES", 4)
    monkeypatch.setattr("agent.collectors.rss_collector._MAX_RETRIES", 1)
    monkeypatch.setattr(
        "agent.collectors.rss_collector.requests.get",
        lambda *_args, **_kwargs: response,
    )
    collector = make_collector(tmp_path, monkeypatch)

    assert collector.collect() == []
    assert response.closed
    assert collector.failed_count == 1
