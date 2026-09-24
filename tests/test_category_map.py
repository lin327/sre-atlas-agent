"""Tests for agent.category_map module."""

from __future__ import annotations

import pytest

from agent.category_map import (
    ALLOWED_CATEGORIES,
    DEFAULT_CATEGORY,
    classify_item,
    validate_category,
)


class TestClassifyItem:
    """Keyword-based classification."""

    @pytest.mark.parametrize(
        "title, expected",
        [
            ("Kubernetes Pod Scheduling Deep Dive", "kubernetes"),
            ("Dockerfile Build Best Practices", "docker"),
            ("Linux Kernel Tuning for SRE", "linux"),
            ("API Gateway and Circuit Breaker Patterns", "architectures"),
            ("Incident Response Playbook", "incidents"),
            ("Runbook: Database Failover", "runbooks"),
            ("Kafka vs RabbitMQ Comparison", "comparisons"),
        ],
    )
    def test_keyword_matching(self, title, expected):
        assert classify_item(title) == expected

    def test_default_category(self):
        assert classify_item("Random unrelated topic") == "runbooks"

    def test_categories_match_wiki_directories(self):
        assert ALLOWED_CATEGORIES == {
            "linux", "docker", "kubernetes", "runbooks", "architectures",
            "incidents", "comparisons",
        }

    def test_tags_contribute_to_classification(self):
        assert classify_item("Guide", tags=["k8s", "helm"]) == "kubernetes"

    def test_case_insensitive(self):
        assert classify_item("KUBERNETES overview") == "kubernetes"

    def test_empty_title_returns_default(self):
        assert classify_item("") == DEFAULT_CATEGORY


class TestValidateCategory:
    """Category validation and normalization."""

    @pytest.mark.parametrize("category", sorted(ALLOWED_CATEGORIES))
    def test_valid_category_passes(self, category):
        assert validate_category(category) == category

    @pytest.mark.parametrize("category", ["nonexistent", "", "../linux", "/docker", "runbooks/../../linux"])
    def test_invalid_category_falls_back(self, category):
        assert validate_category(category) == "runbooks"

    @pytest.mark.parametrize(
        "category, expected",
        [("runbook", "runbooks"), ("architecture", "architectures"), ("  ARCHITECTURE  ", "architectures")],
    )
    def test_legacy_aliases_map_to_wiki_directories(self, category, expected):
        assert validate_category(category) == expected

    def test_whitespace_trimmed(self):
        assert validate_category("  docker  ") == "docker"

    def test_case_normalized(self):
        assert validate_category("KUBERNETES") == "kubernetes"
