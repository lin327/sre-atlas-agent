"""Shared fixtures for SRE Atlas Agent tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# CollectedItem stub (avoids importing collectors.base in tests)
# ---------------------------------------------------------------------------
@dataclass
class CollectedItem:
    title: str
    url: str
    content: str
    source: str = ""
    tags: list[str] = field(default_factory=list)
    published: Optional[str] = None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def tmp_db(tmp_path):
    """Return a path to a temporary SQLite database."""
    return str(tmp_path / "test.db")


@pytest.fixture
def dedup(tmp_db):
    """Return a Dedup instance backed by a temporary database."""
    from agent.dedup import Dedup

    return Dedup(db_path=tmp_db)


@pytest.fixture
def sample_item():
    """Return a minimal CollectedItem for testing."""
    return CollectedItem(
        title="Understanding SLOs",
        url="https://example.com/slo-guide",
        content="SLOs define target reliability levels for services.",
        source="rss",
        tags=["sre", "slo"],
        published="2024-01-15",
    )


@pytest.fixture
def mock_claude_client(monkeypatch):
    """Patch anthropic.Anthropic so no real API calls are made.

    Returns the mock client instance so tests can configure return values.
    """
    mock_client = MagicMock()
    mock_message = MagicMock()
    mock_message.content = [
        MagicMock(
            text=(
                "---\n"
                "title: Test Page\n"
                "description: A test page\n"
                "category: runbook\n"
                "order: 0\n"
                "lastUpdated: 2024-01-15\n"
                "confidence: high\n"
                "sources:\n"
                "  - url: https://example.com\n"
                "    title: Example\n"
                "tags: [test]\n"
                "---\n\n"
                "## Overview\n\n"
                "This is a test page about SRE practices. " * 10
            )
        )
    ]
    mock_client.messages.create.return_value = mock_message

    monkeypatch.setattr("anthropic.Anthropic", MagicMock(return_value=mock_client))

    return mock_client
