"""
SRE Atlas Content Generator

Takes collected items (from RSS/GitHub) and generates structured SRE
documentation using the Claude API.  Output is Chinese-language MDX
with English technical terms preserved, suitable for an Astro wiki.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

import anthropic
import yaml

from agent.category_map import (
    ALLOWED_CATEGORIES,
    CATEGORY_ALIASES,
    classify_item,
    validate_category,
)
from agent.collectors.rss_collector import CollectedItem

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GeneratedPage:
    """Immutable output of a single generation run."""
    slug: str
    title: str
    content: str  # full markdown including frontmatter
    category: str
    confidence: str  # "high" | "medium" | "low"
    source_url: str


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """\
你是一位资深 SRE（Site Reliability Engineering）技术文档撰写专家。
你的任务是根据提供的原始资料，生成结构化的 SRE 技术知识页面（MDX 格式）。

## 输出格式

你 **必须** 以 YAML frontmatter 开头，紧接着是 MDX 正文。

### Frontmatter 规范

```yaml
---
title: <页面标题>
description: <一句话描述，不超过 100 字>
category: <分类>
canonical: false
type: concept | fundamental | runbook | architecture | incident | comparison
order: 0
lastUpdated: <YYYY-MM-DD>
confidence: high | medium | low
sources:
  - url: <来源URL>
    title: <来源标题>
tags: [tag1, tag2, ...]
---
```

**字段说明：**
- `description`: 一句话概括页面内容，用于索引和 SEO
- `category`: 使用提供的分类，不要自行创造新分类
- `canonical`: 固定为 false，生成内容需人工审核
- `type`: 内容类型；目录使用 runbooks / architectures，类型使用 runbook / architecture
- `order`: 排序权重，默认 0
- `lastUpdated`: 生成日期，格式 YYYY-MM-DD

### 正文规范

- 语言：中文为主，英文技术术语保留原文（如 SLO、SLI、Error Budget）
- 结构必须包含以下章节（可根据内容适当增减子章节）：

  ## 概述
  简要说明主题及其在 SRE 实践中的重要性。

  ## 核心概念
  解释关键术语、原理、模型。

  ## 实践
  具体的操作步骤、配置示例、架构图描述。

  ## SRE 要点
  - 与可靠性、可观测性、事件响应的关联
  - 最佳实践与常见反模式
  - 相关指标（SLI/SLO/SLA）

- 使用 wikilinks 引用相关页面：`[[related-slug]]`
- 不要输出占位符文本（如 "待补充"、"TODO"、"lorem ipsum"）
- 不要输出代码围栏包裹的 frontmatter（直接以 --- 开始）
- 正文不能全是列表，必须包含至少 2 段落文本

### 置信度判定

- **high**: 有 2 个以上独立来源互相印证
- **medium**: 仅有单一来源，但内容详实
- **low**: 信息不完整或来源质量存疑
"""


# ---------------------------------------------------------------------------
# Content generator
# ---------------------------------------------------------------------------
class ContentGenerator:
    """Generates structured SRE wiki pages from collected items via Claude API."""

    DEFAULT_MODEL: str = "claude-sonnet-4-6"
    MAX_RETRIES: int = 3
    BASE_BACKOFF: float = 2.0  # seconds
    RATE_LIMIT_DELAY: float = 1.2  # seconds between sequential calls

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model: str = model or self.DEFAULT_MODEL

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def generate_page(self, item: CollectedItem) -> GeneratedPage | None:
        """Generate a single wiki page from a CollectedItem.

        Returns ``None`` when generation fails or the quality gate rejects
        the output.
        """
        if _ISSUE_TITLE.search(item.title.strip()):
            logger.warning("Skipping Issue-like source title: %r", item.title)
            return None
        raw = self._call_claude(item)
        if raw is None:
            return None

        is_valid, issues = validate_content(raw)
        if not is_valid:
            logger.warning(
                "Quality gate failed for %r: %s",
                item.title,
                "; ".join(issues),
            )
            return None

        return self._parse_output(raw, item)

    def generate_batch(self, items: list[CollectedItem]) -> list[GeneratedPage]:
        """Process a list of items sequentially with rate limiting.

        Items that fail generation or the quality gate are silently skipped
        (failures are logged).
        """
        pages: list[GeneratedPage] = []
        total = len(items)

        for idx, item in enumerate(items, start=1):
            logger.info(
                "[%d/%d] Generating page for: %s",
                idx,
                total,
                item.title,
            )
            try:
                page = self.generate_page(item)
            except Exception:
                logger.exception(
                    "Unhandled error generating page for %r", item.title
                )
                page = None

            if page is not None:
                pages.append(page)
                logger.info(
                    "[%d/%d] OK -- slug=%s confidence=%s",
                    idx,
                    total,
                    page.slug,
                    page.confidence,
                )
            else:
                logger.warning("[%d/%d] SKIPPED: %s", idx, total, item.title)

            # Rate limit between calls (skip delay after the last item)
            if idx < total:
                time.sleep(self.RATE_LIMIT_DELAY)

        logger.info(
            "Batch complete: %d/%d pages generated successfully.",
            len(pages),
            total,
        )
        return pages

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _call_claude(self, item: CollectedItem) -> str | None:
        """Send a generation request to the Claude API with retries."""
        user_prompt = self._build_user_prompt(item)

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                message = self._client.messages.create(
                    model=self._model,
                    max_tokens=4096,
                    system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": user_prompt}],
                )
                # Extract text from the first content block.
                return message.content[0].text  # type: ignore[union-attr]

            except (
                anthropic.RateLimitError,
                anthropic.InternalServerError,
                anthropic.APIConnectionError,
                anthropic.APITimeoutError,
            ) as exc:
                wait = self.BASE_BACKOFF ** attempt
                logger.warning(
                    "API error on attempt %d/%d for %r: %s  "
                    "(retrying in %.1fs)",
                    attempt,
                    self.MAX_RETRIES,
                    item.title,
                    exc,
                    wait,
                )
                time.sleep(wait)

            except anthropic.APIStatusError as exc:
                # Non-retryable status errors (400, 401, 403, etc.)
                logger.error(
                    "Non-retryable API error for %r: %s",
                    item.title,
                    exc,
                )
                return None

            except Exception:
                logger.exception(
                    "Unexpected error calling Claude API for %r",
                    item.title,
                )
                return None

        logger.error(
            "All %d attempts exhausted for %r",
            self.MAX_RETRIES,
            item.title,
        )
        return None

    @staticmethod
    def _build_user_prompt(item: CollectedItem) -> str:
        """Compose the user-facing prompt for a single item."""
        parts: list[str] = [
            "请根据以下资料生成一篇 SRE 技术知识页面。\n",
            f"## 原始标题\n{item.title}\n",
            f"## 来源 URL\n{item.url}\n",
            f"## 指定分类\n{_item_category(item)}\n",
        ]

        if item.source:
            parts.append(f"## 来源类型\n{item.source}\n")

        if item.tags:
            parts.append(f"## 相关标签\n{', '.join(item.tags)}\n")

        if item.published:
            parts.append(f"## 发布日期\n{item.published}\n")

        parts.append(f"## 原始内容\n{item.content}\n")

        return "\n".join(parts)

    @staticmethod
    def _parse_output(raw: str, item: CollectedItem) -> GeneratedPage:
        """Extract structured fields from the generated markdown."""
        metadata, body = _split_frontmatter(raw)
        title = metadata.get("title") or item.title
        confidence = str(metadata.get("confidence", "medium")).lower()
        if confidence not in {"high", "medium", "low"}:
            confidence = "low"
        category = _item_category(item)
        slug = _slugify(title)
        page_type = metadata.get("type")
        if page_type not in ("concept", "fundamental", "runbook", "architecture", "incident", "comparison"):
            page_type = {
                "runbooks": "runbook", "architectures": "architecture",
                "incidents": "incident", "comparisons": "comparison",
            }.get(category, "concept")
        metadata.update(title=title, category=category, canonical=False,
                        type=page_type, confidence=confidence)
        content = "---\n" + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False) + "---\n" + body

        return GeneratedPage(
            slug=slug,
            title=title,
            content=content,
            category=category,
            confidence=confidence,
            source_url=item.url,
        )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
# Minimum character count for the markdown body (excluding frontmatter).
_MIN_BODY_LENGTH: int = 200
_VALID_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{2,58}[a-z0-9]")
_ISSUE_TITLE = re.compile(
    r"\b(?:issues?|flaky|bugfix|bug)\b|#\d+\b|^bugfix"
    r"|^(?:fix|fixes)\b|^\[(?:fix|feature request)\]"
    r"|^(?:feat|chore|test|ci|docs|refactor)(?:\([^)]*\))?!?\s*[:：]"
    r"|修复|不稳定测试|测试(?:失败|不稳定)",
    re.IGNORECASE,
)

# Patterns that indicate unfinished / placeholder content.
_PLACEHOLDER_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"待补充", re.IGNORECASE),
    re.compile(r"TODO", re.IGNORECASE),
    re.compile(r"lorem ipsum", re.IGNORECASE),
    re.compile(r"FIXME", re.IGNORECASE),
    re.compile(r"占位", re.IGNORECASE),
    re.compile(r"coming soon", re.IGNORECASE),
]


def validate_content(raw: str, *, slug: str | None = None) -> tuple[bool, list[str]]:
    """Validate generated content against quality criteria.

    Returns ``(is_valid, list_of_issues)`` where *list_of_issues* is empty
    when the content passes all checks.
    """
    issues: list[str] = []

    try:
        metadata, body = _split_frontmatter(raw)
    except (ValueError, TypeError) as exc:
        return False, [str(exc)]
    title = metadata.get("title")
    if not isinstance(title, str) or not title.strip():
        issues.append("Frontmatter is missing a 'title' field or title is empty")
    else:
        if _ISSUE_TITLE.search(title.strip()):
            issues.append("Title resembles a GitHub Issue")
        candidate = _slugify(title) if slug is None else slug
        if not _VALID_SLUG.fullmatch(candidate) or _slugify(candidate) != candidate:
            issues.append("Invalid slug: expected [a-z0-9-]{4,60}")
    body = body.strip()

    # 5. Body must exist and meet minimum length.
    if not body:
        issues.append("Body content is empty")
    elif len(body) < _MIN_BODY_LENGTH:
        issues.append(
            f"Body content is too short ({len(body)} chars, "
            f"minimum {_MIN_BODY_LENGTH})"
        )

    # 6. Must not contain placeholder text.
    for pattern in _PLACEHOLDER_PATTERNS:
        if pattern.search(body):
            issues.append(f"Placeholder text detected: {pattern.pattern!r}")

    prose = re.sub(r"```.*?```|~~~.*?~~~|`[^`\n]*`|<!--.*?-->", "", body, flags=re.DOTALL)
    if not re.search(r"\[\[[a-z0-9-]{4,60}(?:\|[^\]\n]+)?\]\]", prose):
        issues.append("Body has no wikilink")

    return (len(issues) == 0), issues


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------
def _split_frontmatter(raw: str) -> tuple[dict, str]:
    """Parse YAML once per validation/normalization, preserving the body."""
    match = re.search(
        r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)(.*)\Z",
        raw.lstrip(), re.DOTALL,
    )
    if not match:
        raise ValueError("Missing or unclosed YAML frontmatter")
    try:
        metadata = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise ValueError("Invalid YAML frontmatter") from exc
    if not isinstance(metadata, dict):
        raise TypeError("YAML frontmatter must be a mapping")
    return metadata, match.group(2)


def _item_category(item: CollectedItem) -> str:
    """Prefer a recognized source category; never trust generated headings."""
    category = str(getattr(item, "category", "") or "").strip().lower()
    if category in ALLOWED_CATEGORIES or category in CATEGORY_ALIASES:
        return validate_category(category)
    return classify_item(item.title, item.tags)


def _slugify(text: str) -> str:
    """Convert a title into a URL/wiki-friendly slug."""
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9-]+", "-", text)
    text = re.sub(r"\b(?:issues?|flaky|bugfix)\b", "", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")
