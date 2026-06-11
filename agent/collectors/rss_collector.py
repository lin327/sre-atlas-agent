"""RSS feed collector for the SRE Atlas agent.

Reads source URLs from config/sources.yaml, fetches and parses feeds using
feedparser, deduplicates by URL, and returns CollectedItem objects.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import feedparser
import yaml

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "sources.yaml"

_MAX_RETRIES = 3
_INITIAL_BACKOFF_SECONDS = 2.0
_BACKOFF_MULTIPLIER = 2.0


@dataclass
class CollectedItem:
    """A single piece of content gathered by any collector."""

    title: str
    url: str
    source: str
    category: str
    published: Optional[datetime] = None
    summary: str = ""
    content: str = ""
    content_type: str = "rss"  # rss, github, doc
    raw_data: dict = field(default_factory=dict)


class RSSCollector:
    """Fetches and parses RSS feeds defined in a sources YAML config.

    Config format (sources.yaml)::

        rss_sources:
          - url: https://example.com/feed.xml
            category: reliability
            name: Example Blog
    """

    def __init__(self, config_path: Optional[Path] = None) -> None:
        self._config_path = config_path or _DEFAULT_CONFIG_PATH
        self._seen_urls: set[str] = set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def collect(self) -> list[CollectedItem]:
        """Fetch all configured RSS feeds and return new items.

        Items whose URL has already been returned by a previous call are
        skipped (deduplication via an in-memory set).
        """
        sources = self._load_sources()
        if not sources:
            logger.warning("No RSS sources configured in %s", self._config_path)
            return []

        items: list[CollectedItem] = []
        for source in sources:
            url = source.get("url", "")
            name = source.get("name", url)
            category = source.get("category", "general")

            logger.info("Fetching RSS feed: %s (%s)", name, url)
            feed = self._fetch_feed(url, name)
            if feed is None:
                continue

            for entry in feed.entries:
                item = self._entry_to_item(entry, source_name=name, category=category)
                if item.url in self._seen_urls:
                    logger.debug("Skipping duplicate URL: %s", item.url)
                    continue
                self._seen_urls.add(item.url)
                items.append(item)

            logger.info("Collected %d new items from %s", len(feed.entries), name)

        logger.info("RSS collection complete: %d total new items", len(items))
        return items

    def reset(self) -> None:
        """Clear the seen-URLs set so items can be re-collected."""
        self._seen_urls.clear()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_sources(self) -> list[dict]:
        """Load RSS source definitions from the YAML config file."""
        if not self._config_path.exists():
            logger.error("Config file not found: %s", self._config_path)
            return []

        try:
            with open(self._config_path, encoding="utf-8") as fh:
                config = yaml.safe_load(fh) or {}
        except (yaml.YAMLError, OSError) as exc:
            logger.error("Failed to load config %s: %s", self._config_path, exc)
            return []

        sources = config.get("rss_sources", [])
        if not isinstance(sources, list):
            logger.error("rss_sources must be a list in %s", self._config_path)
            return []

        return sources

    def _fetch_feed(self, url: str, name: str) -> Optional[feedparser.FeedParserDict]:
        """Fetch an RSS feed with exponential-backoff retry.

        Returns the parsed feed on success, or ``None`` after persistent
        failure.
        """
        backoff = _INITIAL_BACKOFF_SECONDS

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                feed = feedparser.parse(url)

                # feedparser sets 'bozo' to 1 when there was a parse error
                # but still populates entries when possible.
                if feed.bozo and not feed.entries:
                    raise ValueError(
                        f"Feed parse error: {feed.bozo_exception!r}"
                    )

                logger.debug(
                    "Fetched %d entries from %s", len(feed.entries), name
                )
                return feed

            except Exception as exc:
                logger.warning(
                    "Attempt %d/%d failed for %s: %s",
                    attempt,
                    _MAX_RETRIES,
                    name,
                    exc,
                )
                if attempt < _MAX_RETRIES:
                    logger.info("Retrying in %.1fs ...", backoff)
                    time.sleep(backoff)
                    backoff *= _BACKOFF_MULTIPLIER

        logger.error("All %d attempts failed for %s — skipping", _MAX_RETRIES, name)
        return None

    @staticmethod
    def _entry_to_item(
        entry: feedparser.FeedParserDict,
        *,
        source_name: str,
        category: str,
    ) -> CollectedItem:
        """Convert a single feedparser entry to a ``CollectedItem``."""
        url = entry.get("link", "")
        title = entry.get("title", "(untitled)")

        published = _parse_entry_date(entry)

        summary = entry.get("summary", "")
        # Some feeds put the full content in 'content'; prefer it.
        content_list = entry.get("content", [])
        content = content_list[0].get("value", "") if content_list else ""

        return CollectedItem(
            title=title,
            url=url,
            source=source_name,
            category=category,
            published=published,
            summary=summary,
            content=content or summary,
            content_type="rss",
            raw_data=dict(entry),
        )


def _parse_entry_date(entry: feedparser.FeedParserDict) -> Optional[datetime]:
    """Best-effort extraction of a published datetime from a feed entry."""
    # feedparser normalises date fields into a time.struct_time called
    # 'published_parsed' (or 'updated_parsed' as fallback).
    struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if struct is None:
        return None
    try:
        return datetime(*struct[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
