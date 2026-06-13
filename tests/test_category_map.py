"""Tests for agent.category_map module."""

from __future__ import annotations

import pytest

from agent.category_map import classify_item, validate_category, DEFAULT_CATEGORY


class TestClassifyItem:
    """Keyword-based classification."""

    @pytest.mark.parametrize(
        "title, expected",
        [
            ("Kubernetes Pod Scheduling Deep Dive", "kubernetes"),
            ("Dockerfile Build Best Practices", "docker"),
            ("Linux Kernel Tuning for SRE", "linux"),
            ("API Gateway and Circuit Breaker Patterns", "architecture"),
            ("Incident Response Playbook", "incidents"),
            ("Runbook: Database Failover", "runbook"),
            ("Kafka vs RabbitMQ Comparison", "comparisons"),
        ],
    )
    def test_keyword_matching(self, title, expected):
        assert classify_item(title) == expected

    def test_default_category(self):
        assert classify_item("Random unrelated topic") == DEFAULT_CATEGORY

    def test_tags_contribute_to_classification(self):
        assert classify_item("Guide", tags=["k8s", "helm"]) == "kubernetes"

    def test_case_insensitive(self):
        assert classify_item("KUBERNETES overview") == "kubernetes"

    def test_empty_title_returns_default(self):
        assert classify_item("") == DEFAULT_CATEGORY


class TestValidateCategory:
    """Category validation and normalization."""

    def test_valid_category_passes(self):
        assert validate_category("kubernetes") == "kubernetes"

    def test_invalid_category_falls_back(self):
        assert validate_category("nonexistent") == DEFAULT_CATEGORY

    def test_whitespace_trimmed(self):
        assert validate_category("  docker  ") == "docker"

    def test_case_normalized(self):
        assert validate_category("KUBERNETES") == "kubernetes"
