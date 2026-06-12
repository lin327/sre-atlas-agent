"""GitHub Issues/PRs collector for the SRE Atlas agent.

Uses the GitHub REST API to fetch recent issues (and pull requests) from
configured repositories, optionally filtered by labels.  Deduplicates by
issue URL and returns ``CollectedItem`` objects.

Requires:
    - ``requests`` library
    - Optionally a ``GITHUB_TOKEN`` environment variable for higher rate limits
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests
import yaml

from agent.collectors.rss_collector import CollectedItem

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "sources.yaml"
_GITHUB_API_BASE = "https://api.github.com"
_DEFAULT_LOOKBACK_DAYS = 30
_PER_PAGE = 100  # max allowed by GitHub


class GitHubCollector:
    """Fetches recent GitHub issues/PRs for configured repositories.

    Config format (sources.yaml)::

        github_sources:
          - repo: owner/repo-name
            labels:
              - bug
              - incident
            category: incidents
    """

    def __init__(self, config_path: Optional[Path] = None, token: Optional[str] = None) -> None:
        self._config_path = config_path or _DEFAULT_CONFIG_PATH
        self._token = token or os.environ.get("GITHUB_TOKEN")
        self._session = self._build_session()
        self._seen_urls: set[str] = set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def collect(self, lookback_days: int = _DEFAULT_LOOKBACK_DAYS) -> list[CollectedItem]:
        """Fetch issues from all configured repos and return new items.

        Items whose URL has already been returned by a previous call are
        skipped (deduplication via an in-memory set).
        """
        sources = self._load_sources()
        if not sources:
            logger.warning("No GitHub sources configured in %s", self._config_path)
            return []

        since = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        items: list[CollectedItem] = []

        for source in sources:
            repo = source.get("repo", "")
            if not repo:
                logger.warning("Skipping source with no 'repo' field")
                continue

            labels = source.get("labels", [])
            _category = source.get("category", "github")

            logger.info("Fetching issues from %s (labels=%s)", repo, labels)
            issues = self._fetch_issues(repo, labels=labels, since=since)
            for issue in issues:
                if issue.url in self._seen_urls:
                    logger.debug("Skipping duplicate URL: %s", issue.url)
                    continue
                self._seen_urls.add(issue.url)
                items.append(issue)

            logger.info("Collected %d new items from %s", len(issues), repo)

        logger.info("GitHub collection complete: %d total new items", len(items))
        return items

    def reset(self) -> None:
        """Clear the seen-URLs set so items can be re-collected."""
        self._seen_urls.clear()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_session(self) -> requests.Session:
        """Build a ``requests.Session`` with auth and accept headers."""
        session = requests.Session()
        session.headers.update({
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        if self._token:
            session.headers["Authorization"] = f"Bearer {self._token}"
        else:
            logger.warning(
                "GITHUB_TOKEN not set — API requests are limited to 60/hour"
            )
        return session

    def _load_sources(self) -> list[dict]:
        """Load GitHub source definitions from the YAML config file."""
        if not self._config_path.exists():
            logger.error("Config file not found: %s", self._config_path)
            return []

        try:
            with open(self._config_path, encoding="utf-8") as fh:
                config = yaml.safe_load(fh) or {}
        except (yaml.YAMLError, OSError) as exc:
            logger.error("Failed to load config %s: %s", self._config_path, exc)
            return []

        sources = config.get("github_sources", [])
        if not isinstance(sources, list):
            logger.error("github_sources must be a list in %s", self._config_path)
            return []

        return sources

    def _fetch_issues(
        self,
        repo: str,
        *,
        labels: list[str],
        since: datetime,
    ) -> list[CollectedItem]:
        """Fetch paginated issues for a single repository.

        Returns an *unfiltered* list of ``CollectedItem`` objects; the caller
        is responsible for deduplication.
        """
        url = f"{_GITHUB_API_BASE}/repos/{repo}/issues"
        params: dict = {
            "state": "all",
            "since": since.isoformat(),
            "per_page": _PER_PAGE,
            "sort": "updated",
            "direction": "desc",
        }
        if labels:
            params["labels"] = ",".join(labels)

        items: list[CollectedItem] = []
        page = 1

        while True:
            params["page"] = page
            logger.debug("GET %s page=%d", url, page)

            response = self._request_with_backoff(url, params)
            if response is None:
                break

            data = response.json()
            if not data:
                break

            for issue in data:
                item = self._issue_to_item(issue, repo=repo)
                items.append(item)

            # Stop if we've gone past the lookback window or hit the last page.
            if not self._has_next_page(response):
                break

            page += 1

        return items

    def _request_with_backoff(
        self,
        url: str,
        params: dict,
        max_retries: int = 3,
    ) -> Optional[requests.Response]:
        """Issue a GET request with exponential backoff and rate-limit handling."""
        backoff = 2.0

        for attempt in range(1, max_retries + 1):
            try:
                response = self._session.get(url, params=params, timeout=30)

                # Handle rate limiting
                if response.status_code == 403 and "rate limit" in response.text.lower():
                    reset_epoch = int(response.headers.get("X-RateLimit-Reset", 0))
                    wait = max(reset_epoch - time.time(), 1)
                    logger.warning(
                        "Rate limited — waiting %.0fs for reset", wait
                    )
                    time.sleep(wait)
                    continue

                response.raise_for_status()
                self._log_rate_limit(response)
                return response

            except requests.RequestException as exc:
                logger.warning(
                    "Attempt %d/%d failed for %s: %s",
                    attempt,
                    max_retries,
                    url,
                    exc,
                )
                if attempt < max_retries:
                    logger.info("Retrying in %.1fs ...", backoff)
                    time.sleep(backoff)
                    backoff *= 2

        logger.error("All %d attempts failed for %s — skipping", max_retries, url)
        return None

    @staticmethod
    def _has_next_page(response: requests.Response) -> bool:
        """Check the Link header for a next-page relation."""
        link_header = response.headers.get("Link", "")
        return 'rel="next"' in link_header

    @staticmethod
    def _log_rate_limit(response: requests.Response) -> None:
        """Log remaining rate-limit quota if headers are present."""
        remaining = response.headers.get("X-RateLimit-Remaining")
        limit = response.headers.get("X-RateLimit-Limit")
        if remaining is not None and limit is not None:
            logger.debug("GitHub API rate limit: %s/%s remaining", remaining, limit)
            if int(remaining) < 10:
                logger.warning(
                    "GitHub API rate limit nearly exhausted: %s remaining",
                    remaining,
                )

    @staticmethod
    def _issue_to_item(issue: dict, *, repo: str) -> CollectedItem:
        """Convert a GitHub issue JSON object to a ``CollectedItem``."""
        title = issue.get("title", "(untitled)")
        url = issue.get("html_url", "")
        body = issue.get("body", "") or ""
        labels = [lbl.get("name", "") for lbl in issue.get("labels", [])]

        created_at_str = issue.get("created_at")
        created_at = None
        if created_at_str:
            try:
                created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

        # Include whether this is a PR (GitHub marks PRs with a 'pull_request' key).
        is_pr = "pull_request" in issue
        content_type = "github_pr" if is_pr else "github_issue"

        return CollectedItem(
            title=title,
            url=url,
            source=repo,
            category="github",
            published=created_at,
            summary=body[:300] if body else "",
            content=body,
            content_type=content_type,
            raw_data={
                "labels": labels,
                "state": issue.get("state"),
                "is_pr": is_pr,
                "number": issue.get("number"),
                "user": issue.get("user", {}).get("login"),
            },
        )
