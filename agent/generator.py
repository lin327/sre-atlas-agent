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
from dataclasses import dataclass, field
from typing import Optional

import anthropic

# ---------------------------------------------------------------------------
# CollectedItem import -- graceful fallback for standalone testing
# ---------------------------------------------------------------------------
try:
    from collectors.base import CollectedItem
except ImportError:
    @dataclass
    class CollectedItem:  # type: ignore[no-redef]
        """Fallback stub so this module can be tested independently."""
        title: str
        url: str
        content: str
        source: str = ""
        tags: list[str] = field(default_factory=list)
        published: Optional[str] = None


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

    DEFAULT_MODEL: str = "mimo-v2.5"
    MAX_RETRIES: int = 3
    BASE_BACKOFF: float = 2.0  # seconds
    RATE_LIMIT_DELAY: float = 1.2  # seconds between sequential calls

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> None:
        from config.settings import ANTHROPIC_BASE_URL
        effective_base_url = base_url or ANTHROPIC_BASE_URL or None
        # Anthropic SDK appends /v1/messages automatically; strip trailing /v1
        # if the user's base_url already includes it to avoid /v1/v1 duplication.
        if effective_base_url and effective_base_url.rstrip("/").endswith("/v1"):
            effective_base_url = effective_base_url.rstrip("/")[:-3]
        self._client = anthropic.Anthropic(
            api_key=api_key,
            base_url=effective_base_url,
        )
        self._model: str = model or self.DEFAULT_MODEL

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def generate_page(self, item: CollectedItem) -> Optional[GeneratedPage]:
        """Generate a single wiki page from a CollectedItem.

        Returns ``None`` when generation fails or the quality gate rejects
        the output.
        """
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
    def _call_claude(self, item: CollectedItem) -> Optional[str]:
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
        title = _extract_frontmatter_field(raw, "title") or item.title
        confidence = (
            _extract_frontmatter_field(raw, "confidence") or "medium"
        )
        category = _extract_category(raw)
        slug = _slugify(title)

        return GeneratedPage(
            slug=slug,
            title=title,
            content=raw,
            category=category,
            confidence=confidence,
            source_url=item.url,
        )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
# Minimum character count for the markdown body (excluding frontmatter).
_MIN_BODY_LENGTH: int = 200

# Patterns that indicate unfinished / placeholder content.
_PLACEHOLDER_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"待补充", re.IGNORECASE),
    re.compile(r"TODO", re.IGNORECASE),
    re.compile(r"lorem ipsum", re.IGNORECASE),
    re.compile(r"FIXME", re.IGNORECASE),
    re.compile(r"占位", re.IGNORECASE),
    re.compile(r"coming soon", re.IGNORECASE),
]


def validate_content(raw: str) -> tuple[bool, list[str]]:
    """Validate generated content against quality criteria.

    Returns ``(is_valid, list_of_issues)`` where *list_of_issues* is empty
    when the content passes all checks.
    """
    issues: list[str] = []

    # 1. Must start with frontmatter delimiters.
    stripped = raw.lstrip()
    if not stripped.startswith("---"):
        issues.append("Missing YAML frontmatter (must start with ---)")
        # Cannot reliably check further without frontmatter.
        return False, issues

    # 2. Must have a closing --- for the frontmatter block.
    second_delim = stripped.find("---", 3)
    if second_delim == -1:
        issues.append("Frontmatter block is not closed (missing second ---)")
        return False, issues

    frontmatter = stripped[3:second_delim].strip()

    # 3. Must contain a non-empty title field.
    title_match = re.search(r"^title:\s*(.+)$", frontmatter, re.MULTILINE)
    if not title_match or not title_match.group(1).strip():
        issues.append("Frontmatter is missing a 'title' field or title is empty")

    # 4. Extract body (everything after the second ---).
    body = stripped[second_delim + 3 :].strip()

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

    return (len(issues) == 0), issues


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------
def _extract_frontmatter_field(raw: str, field_name: str) -> Optional[str]:
    """Pull a single scalar value from YAML frontmatter."""
    match = re.search(
        rf"^---\s*\n.*?^{field_name}:\s*(.+?)\s*$.*?^---",
        raw,
        re.MULTILINE | re.DOTALL,
    )
    if match:
        value = match.group(1).strip().strip("\"'")
        return value if value else None
    return None


def _extract_category(raw: str) -> str:
    """Derive a category from the first H2 heading in the body."""
    # Skip frontmatter.
    match = re.search(r"^---\s*\n.*?^---", raw, re.MULTILINE | re.DOTALL)
    body = raw[match.end() :] if match else raw

    heading = re.search(r"^##\s+(.+)$", body, re.MULTILINE)
    return heading.group(1).strip() if heading else "general"


def _slugify(text: str) -> str:
    """Convert a title into a URL/wiki-friendly slug."""
    # Preserve CJK characters, replace spaces/punctuation with hyphens.
    text = text.lower().strip()
    text = re.sub(r"[\s/_]+", "-", text)
    text = re.sub(r"[^\w一-鿿-]", "", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-") or "untitled"
