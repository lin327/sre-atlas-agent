"""GitHub collection contract tests; all HTTP requests are mocked."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
import requests
import yaml

from agent.collectors.github_collector import GitHubCollector


@pytest.fixture(autouse=True)
def mock_get(monkeypatch):
    get = MagicMock()
    monkeypatch.setattr(requests.Session, "get", get)
    return get


def response(items):
    result = MagicMock(spec=requests.Response)
    result.status_code = 200
    result.headers = {}
    result.json.return_value = items
    return result


def issue(repo):
    return {
        "title": "Database recovery procedure",
        "html_url": f"https://github.com/{repo}/issues/1",
        "body": "Check the recovery logs.",
        "created_at": "2026-01-01T00:00:00Z",
        "labels": [{"name": "incident"}],
    }


def config_path(tmp_path, sources):
    path = tmp_path / "sources.yaml"
    path.write_text(yaml.safe_dump({"github": sources}), encoding="utf-8")
    return path


def test_configured_categories_reach_collected_items(tmp_path, mock_get):
    sources = [
        {"repo": "example/runbooks", "category": "runbook", "labels": ["incident"]},
        {"repo": "example/designs", "category": "architectures", "labels": []},
    ]
    mock_get.side_effect = [response([issue(source["repo"])]) for source in sources]
    collector = GitHubCollector(config_path(tmp_path, sources), token="test-token")

    items = collector.collect()

    assert [item.category for item in items] == ["runbook", "architectures"]
    assert [item.source for item in items] == [source["repo"] for source in sources]
    assert all(item.content_type == "github_issue" for item in items)
    assert all(item.published == datetime(2026, 1, 1, tzinfo=UTC) for item in items)
    assert mock_get.call_count == 2
    assert mock_get.call_args_list[0].kwargs["params"]["labels"] == "incident"


def test_missing_token_warns_and_collects_anonymously(tmp_path, mock_get, monkeypatch, caplog):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    repo = "example/recovery"
    mock_get.return_value = response([issue(repo)])
    path = config_path(tmp_path, [{"repo": repo, "category": "incidents"}])

    collector = GitHubCollector(path)
    items = collector.collect()

    assert "GITHUB_TOKEN not set" in caplog.text
    assert "Authorization" not in collector._session.headers
    assert len(items) == 1
    assert items[0].category == "incidents"
    mock_get.assert_called_once()


def test_missing_category_is_left_for_generator_classification(tmp_path, mock_get):
    repo = "example/recovery"
    mock_get.return_value = response([issue(repo)])
    path = config_path(tmp_path, [{"repo": repo}])

    items = GitHubCollector(path, token="test-token").collect()

    assert len(items) == 1
    assert items[0].category == ""


@pytest.mark.parametrize("contents", ["", "{}", "github: []"])
def test_empty_configuration_skips_http(tmp_path, mock_get, contents):
    path = tmp_path / "sources.yaml"
    path.write_text(contents, encoding="utf-8")

    assert GitHubCollector(path, token="test-token").collect() == []
    mock_get.assert_not_called()


def test_empty_api_result_returns_no_items(tmp_path, mock_get):
    mock_get.return_value = response([])
    path = config_path(tmp_path, [{"repo": "example/recovery", "category": "incidents"}])

    assert GitHubCollector(path, token="test-token").collect() == []
    mock_get.assert_called_once()
