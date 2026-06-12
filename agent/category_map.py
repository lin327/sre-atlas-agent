"""Category classification mapping for SRE Atlas Agent.

Maps keywords found in item titles and tags to predefined categories
for consistent wiki page organization.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Keyword → Category mapping
# ---------------------------------------------------------------------------
CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "kubernetes": [
        "kubernetes", "k8s", "container", "pod", "deployment", "service",
        "replicaset", "statefulset", "daemonset", "ingress", "configmap",
        "secret", "namespace", "helm", "kubectl", "kubelet",
    ],
    "docker": [
        "docker", "container", "image", "dockerfile", "docker-compose",
        "registry", "swarm",
    ],
    "linux": [
        "linux", "kernel", "systemd", "cron", "shell", "bash", "ubuntu",
        "centos", "debian", "rhel", "systemctl", "journalctl", "iptables",
        "selinux", "apparmor",
    ],
    "architecture": [
        "microservice", "monolith", "distributed", "scalability", "load balancing",
        "service mesh", "api gateway", "event-driven", "cqrs", "event sourcing",
        "circuit breaker", "retry", "timeout",
    ],
    "incidents": [
        "incident", "outage", "postmortem", "rca", "root cause", "blameless",
        "escalation", "oncall", "on-call", "pager", "alert",
    ],
    "runbook": [
        "runbook", "playbook", "procedure", "checklist", "troubleshooting",
        "debug", "diagnostic", "health check",
    ],
    "comparisons": [
        "vs", "versus", "comparison", "alternative", "benchmark", "trade-off",
        "pros and cons",
    ],
}

# ---------------------------------------------------------------------------
# Allowed categories (including default)
# ---------------------------------------------------------------------------
ALLOWED_CATEGORIES: set[str] = set(CATEGORY_KEYWORDS.keys()) | {"runbook"}

# Default category when no keywords match
DEFAULT_CATEGORY: str = "runbook"


def classify_item(title: str, tags: list[str] | None = None) -> str:
    """Pre-classify an item based on title and tags keywords.

    Scans the combined text of title and tags for keyword matches.
    Returns the first matching category, or ``DEFAULT_CATEGORY`` if no
    keywords match.

    Parameters
    ----------
    title : str
        The item title.
    tags : list[str] | None
        Optional list of tags associated with the item.

    Returns
    str
        The classified category name.
    """
    text = title.lower()
    if tags:
        text = f"{text} {' '.join(tags)}".lower()

    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return category

    return DEFAULT_CATEGORY


def get_category(title: str, tags: list[str] | None = None) -> str:
    """Return the category for a given title and optional tags.

    Alias for :func:`classify_item`.
    """
    return classify_item(title, tags)


def validate_category(category: str) -> str:
    """Validate and normalize a category string.

    Returns the category if it's in the allowed set, otherwise returns
    ``DEFAULT_CATEGORY``.
    """
    normalized = category.lower().strip()
    return normalized if normalized in ALLOWED_CATEGORIES else DEFAULT_CATEGORY
