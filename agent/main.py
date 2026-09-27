"""SRE Atlas Agent -- main entry point.

Loads configuration, wires the existing RSS/GitHub collectors and the
Claude-based wiki generator together, then runs the full
collect -> dedup -> generate -> output pipeline either once or on a
repeating schedule.

Usage
-----
    # Single run (e.g. in CI)
    python -m agent.main --once

    # Continuous mode (default 6-hour interval)
    python -m agent.main

    # Preview collected items without Claude API calls or writes
    python -m agent.main --once --dry-run

    # Paid preview: generate MDX without database writes
    python -m agent.main --once --dry-run --generate-anyway

    # Custom config / output / interval
    python -m agent.main --config config/sources.yaml --output output/ --interval 4
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml

from agent.category_map import ALLOWED_CATEGORIES
from agent.dedup import Dedup, DeduplicationError
from agent.scheduler import CollectionScheduler
from config.settings import MAX_INPUT_CHARS, MAX_ITEMS_PER_SOURCE, MAX_PAGES_PER_RUN

# ---------------------------------------------------------------------------
# Heavy imports -- deferred so ``--help`` works without all deps installed.
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict[str, Any]:
    """Load and validate a ``sources.yaml`` file.

    Raises ``FileNotFoundError`` or ``TypeError`` on problems.
    """
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with config_path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    if not isinstance(data, dict):
        raise TypeError("Config must be a YAML mapping.")

    return data


def normalize_source_url(url: str) -> str | None:
    """Normalize an absolute HTTP(S) URL for cross-collector deduplication."""
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parts = urlsplit(url.strip())
        scheme = parts.scheme.lower()
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if scheme not in {"http", "https"} or not hostname or parts.username or parts.password:
        return None

    hostname = hostname.lower()
    authority = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        authority = f"{authority}:{port}"
    return urlunsplit((scheme, authority, parts.path or "/", parts.query, ""))


def _item_input_chars(item: Any) -> int:
    """Count text fields passed to the model so source payloads stay bounded."""
    values = (
        item.title,
        item.url,
        item.source,
        item.category,
        item.summary,
        item.content,
        *(item.tags or []),
    )
    return sum(len(value) for value in values if isinstance(value, str))


@dataclass
class ItemSelection:
    items: list[Any]
    budget_hit: int = 0


def select_items_for_generation(
    items: list[Any],
    *,
    dedup: Dedup | None = None,
    dry_run: bool = False,
    max_items_per_source: int = MAX_ITEMS_PER_SOURCE,
    max_pages_per_run: int = MAX_PAGES_PER_RUN,
    max_input_chars: int = MAX_INPUT_CHARS,
) -> ItemSelection:
    """Normalize and deduplicate collected items before applying spend limits."""
    normalized_by_url: dict[str, Any] = {}
    input_lengths: dict[str, int] = {}

    for item in items:
        url = normalize_source_url(item.url)
        if url is None:
            logger.warning("Skipping item with invalid source URL: %r", item.title)
            continue
        normalized_item = replace(item, url=url)
        input_chars = _item_input_chars(normalized_item)
        if url in normalized_by_url:
            logger.info("Skipping duplicate normalized URL: %s", url)
            if input_lengths[url] > max_input_chars >= input_chars:
                normalized_by_url[url] = normalized_item
                input_lengths[url] = input_chars
            continue
        normalized_by_url[url] = normalized_item
        input_lengths[url] = input_chars

    # URL normalization and cross-collector deduplication happen before either
    # input or API-call budgets are enforced.
    normalized_items: list[Any] = []
    already_seen = 0
    oversized = 0
    for url, item in normalized_by_url.items():
        if input_lengths[url] > max_input_chars:
            oversized += 1
            logger.warning(
                "Skipping oversized source item %r (%d chars; limit %d).",
                item.title,
                input_lengths[url],
                max_input_chars,
            )
            continue
        if dedup and not dry_run and dedup.is_seen(url):
            already_seen += 1
            continue
        normalized_items.append(item)

    selected: list[Any] = []
    source_counts: Counter[str] = Counter()
    source_limited = 0
    run_limited = 0
    for index, item in enumerate(normalized_items):
        if len(selected) >= max_pages_per_run:
            logger.warning(
                "[budget_hit] MAX_PAGES_PER_RUN=%d reached; remaining candidates skipped.",
                max_pages_per_run,
            )
            run_limited = len(normalized_items) - index
            break
        source = item.source.strip() or "unknown"
        if source_counts[source] >= max_items_per_source:
            source_limited += 1
            logger.info(
                "[budget_hit] MAX_ITEMS_PER_SOURCE=%d reached for %s; skipping %r.",
                max_items_per_source,
                source,
                item.title,
            )
            continue
        source_counts[source] += 1
        selected.append(item)

    budget_hit = oversized + source_limited + run_limited
    logger.info(
        "Selected %d/%d candidate(s) for generation (%d already seen, %d oversized, %d source-capped).",
        len(selected),
        len(items),
        already_seen,
        oversized,
        source_limited,
    )
    if budget_hit:
        logger.warning(
            "[budget_hit] skipped=%d oversized=%d source_limited=%d run_limited=%d.",
            budget_hit,
            oversized,
            source_limited,
            run_limited,
        )
    return ItemSelection(items=selected, budget_hit=budget_hit)


@dataclass
class PipelineResult:
    collected: int = 0
    selected: int = 0
    generated: int = 0
    reused: int = 0
    written: int = 0
    skipped: int = 0
    failed: int = 0
    budget_hit: int = 0

    @property
    def status(self) -> str:
        if self.failed:
            return "failed"
        return "written" if self.written else "no_updates"


def _write_page_atomically(output_dir: Path, page: Any) -> tuple[str, Path]:
    """Publish a complete MDX file without exposing partial writes or overwrites."""
    category_dir = output_dir / page.category
    category_dir.mkdir(parents=True, exist_ok=True)
    number = 1

    while True:
        suffix = "" if number == 1 else f"-{number}"
        slug = f"{page.slug[:60 - len(suffix)].rstrip('-')}{suffix}"
        target = category_dir / f"{slug}.mdx"
        if target.is_file():
            if target.read_text(encoding="utf-8") == page.content:
                return slug, target
            number += 1
            continue

        temporary_path: Path | None = None
        try:
            with NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=category_dir,
                prefix=".pending-", suffix=".mdx", delete=False,
            ) as temporary:
                temporary.write(page.content)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            try:
                os.link(temporary_path, target)
                return slug, target
            except FileExistsError:
                if target.is_file() and target.read_text(encoding="utf-8") == page.content:
                    return slug, target
                number += 1
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def _record_github_output(result: PipelineResult) -> None:
    """Expose a stable summary to the collection workflow when running in CI."""
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with Path(output_path).open("a", encoding="utf-8") as output:
        output.write(f"result={result.status}\n")
        output.write(f"written={result.written}\n")
        output.write(f"has_written={str(result.written > 0).lower()}\n")
        output.write(f"failed={result.failed}\n")


def _log_pipeline_summary(result: PipelineResult) -> None:
    """Emit one machine-readable key-value record for each collection run."""
    logger.info(
        "pipeline_summary collected=%d selected=%d generated=%d reused=%d "
        "written=%d skipped=%d failed=%d budget_hit=%d status=%s",
        result.collected,
        result.selected,
        result.generated,
        result.reused,
        result.written,
        result.skipped,
        result.failed,
        result.budget_hit,
        result.status,
    )


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class AtlasPipeline:
    """Orchestrate one full collect -> dedup -> generate -> output cycle.

    Parameters
    ----------
    config : dict
        Parsed ``sources.yaml`` content.
    config_path : str
        Path to the YAML config file (passed to collectors that self-load).
    output_dir : str
        Output root; drafts go under ``inbox/`` unless PUBLISH_CANONICAL=true.
    dry_run : bool
        Preview collected items without Claude calls, MDX or database writes.
    dedup : Dedup | None
        Dedup instance.  Ignored when *dry_run* is *True*.
    generate_anyway : bool
        Opt into paid MDX generation during dry-run; database remains untouched.
    """

    def __init__(
        self,
        config: dict[str, Any],
        config_path: str = "config/sources.yaml",
        output_dir: str = "output",
        dry_run: bool = False,
        dedup: Dedup | None = None,
        generate_anyway: bool = False,
    ) -> None:
        if generate_anyway and not dry_run:
            raise ValueError("--generate-anyway requires --dry-run")
        self._config = config
        self._config_path = Path(config_path)
        self._output_dir = Path(output_dir)
        if os.getenv("PUBLISH_CANONICAL", "false") != "true":
            parts = self._output_dir.resolve().parts
            if any(parts[i:i + 2] == ("src", "pages") for i in range(len(parts) - 1)):
                raise ValueError("Draft output must not be under src/pages; use output/ instead.")
            self._output_dir /= "inbox"
        self._dry_run = dry_run
        self._dedup = dedup
        self._generate_anyway = generate_anyway

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def run(self) -> PipelineResult:
        """Execute one cycle, keeping each generated item recoverable."""
        from agent.collectors.github_collector import GitHubCollector
        from agent.collectors.rss_collector import CollectedItem, RSSCollector
        from agent.generator import (
            ContentGenerator,
            GeneratedPage,
            _item_category,
            validate_content,
        )

        result = PipelineResult()

        # 1. Collect -------------------------------------------------------
        logger.info("Step 1/5 -- Collecting items...")
        all_items: list[CollectedItem] = []

        if self._config.get("rss"):
            rss_collector = RSSCollector(config_path=self._config_path)
            all_items.extend(rss_collector.collect())
            result.failed += rss_collector.failed_count

        if self._config.get("github"):
            gh_collector = GitHubCollector(config_path=self._config_path)
            all_items.extend(gh_collector.collect())
            result.failed += gh_collector.failed_count

        logger.info("Collected %d item(s) total.", len(all_items))
        result.collected = len(all_items)
        if not all_items:
            logger.info("Nothing collected -- cycle complete.")
            _log_pipeline_summary(result)
            return result

        # 2. Normalize, deduplicate, and enforce spend limits --------------
        logger.info("Step 2/5 -- Deduplicating and applying generation budgets...")
        selection = select_items_for_generation(
            all_items,
            dedup=self._dedup,
            dry_run=self._dry_run,
        )
        new_items = selection.items
        result.budget_hit = selection.budget_hit
        result.selected = len(new_items)
        if not new_items:
            logger.info("No new items -- cycle complete.")
            _log_pipeline_summary(result)
            return result

        if self._dry_run:
            for item in new_items:
                logger.info("[dry-run] [%s] %s (%s)", _item_category(item), item.title, item.url)
            if not self._generate_anyway:
                logger.info("[dry-run] 仅预览采集与分类；不调用 Claude，不写 MDX 或数据库。")
                _log_pipeline_summary(result)
                return result
            logger.warning("[dry-run --generate-anyway] 将调用付费 Claude API 并写 MDX，不写数据库。")

        # 3–5. Generate, persist, write, then mark seen per item. ---------
        logger.info("Processing %d selected item(s) one at a time...", len(new_items))
        generator: ContentGenerator | None = None
        for item in new_items:
            page: GeneratedPage | None = None
            cached: dict[str, str] | None = None
            if self._dedup and not self._dry_run:
                try:
                    cached_result = self._dedup.get_generated_result(item.url)
                    if isinstance(cached_result, dict):
                        cached = cached_result
                except Exception:
                    result.failed += 1
                    logger.exception("Failed to load cached generation for %s", item.url)
                    continue

            if cached is not None:
                page = GeneratedPage(
                    slug=cached["slug"],
                    title=cached["title"],
                    content=cached["content"],
                    category=cached["category"],
                    confidence=cached["confidence"],
                    source_url=cached["source_url"],
                )
                result.reused += 1
                logger.info("Reusing persisted generation for %s", item.url)
            else:
                if ContentGenerator.should_skip_item(item):
                    result.skipped += 1
                    continue
                try:
                    if generator is None:
                        generator = ContentGenerator()
                    page = generator.generate_page(item)
                except Exception:
                    result.failed += 1
                    logger.exception("Failed to generate page for %s", item.url)
                    continue
                if page is None:
                    result.failed += 1
                    logger.error("Generation or quality validation failed for %s", item.url)
                    continue
                result.generated += 1

            valid, issues = validate_content(page.content, slug=page.slug)
            if not valid or page.category not in ALLOWED_CATEGORIES:
                result.failed += 1
                logger.error(
                    "Refusing invalid generated output %r (%s): %s",
                    page.slug,
                    page.category,
                    issues,
                )
                continue

            if self._dedup and not self._dry_run and cached is None:
                try:
                    cached_result = self._dedup.persist_generated_result(
                        source_url=item.url,
                        source=item.source or "unknown",
                        slug=page.slug,
                        title=page.title,
                        category=page.category,
                        confidence=page.confidence,
                        content=page.content,
                    )
                    if isinstance(cached_result, dict):
                        page = GeneratedPage(
                            slug=cached_result["slug"],
                            title=cached_result["title"],
                            content=cached_result["content"],
                            category=cached_result["category"],
                            confidence=cached_result["confidence"],
                            source_url=cached_result["source_url"],
                        )
                except Exception:
                    result.failed += 1
                    logger.exception("Failed to persist generation for %s", item.url)
                    continue

            try:
                slug, output_file = _write_page_atomically(self._output_dir, page)
            except Exception:
                result.failed += 1
                logger.exception("Failed to write generated page for %s", item.url)
                continue
            if slug != page.slug:
                logger.info("Resolved slug collision: %s -> %s", page.slug, slug)
            result.written += 1
            logger.debug("Wrote: %s", output_file)

            if self._dedup and not self._dry_run:
                try:
                    self._dedup.mark_seen(
                        url=item.url,
                        source=item.source or "unknown",
                        category=page.category,
                        title=page.title,
                    )
                except Exception:
                    result.failed += 1
                    logger.exception("Failed to mark seen after writing %s", item.url)

        if self._dry_run:
            logger.info("[dry-run] Skipping database writes.")
        _log_pipeline_summary(result)
        return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sre-atlas-agent",
        description="SRE Atlas Agent -- automated wiki generation from SRE sources.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single collection cycle and exit.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="预览采集条目与分类；不调用 Claude，不写 MDX 或数据库。",
    )
    parser.add_argument(
        "--generate-anyway",
        action="store_true",
        help="配合 --dry-run 调用付费 Claude API 并写 MDX，仍不写数据库。",
    )
    parser.add_argument(
        "--config",
        default="config/sources.yaml",
        help="Path to the sources configuration file (default: config/sources.yaml).",
    )
    parser.add_argument(
        "--output",
        default="output",
        help="Output root (default: output/); drafts use inbox/ unless PUBLISH_CANONICAL=true.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=6,
        help="Hours between collection cycles in continuous mode (default: 6).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.  Returns an exit code (0 = success)."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.generate_anyway and not args.dry_run:
        parser.error("--generate-anyway requires --dry-run")

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s -- %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Load config -----------------------------------------------------------
    try:
        config = load_config(args.config)
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 1
    except (ValueError, TypeError) as exc:
        logger.error("Invalid config: %s", exc)
        return 1

    # Initialise dedup ------------------------------------------------------
    dedup: Dedup | None = None
    if not args.dry_run:
        try:
            dedup = Dedup()
        except DeduplicationError as exc:
            logger.error("Dedup init failed: %s", exc)
            return 1
    else:
        logger.info("[dry-run] Database connection skipped.")

    # Build pipeline --------------------------------------------------------
    try:
        pipeline = AtlasPipeline(
            config=config,
            config_path=args.config,
            output_dir=args.output,
            dry_run=args.dry_run,
            dedup=dedup,
            generate_anyway=args.generate_anyway,
        )
    except ValueError as exc:
        logger.error("Invalid output configuration: %s", exc)
        return 1

    if args.once:
        try:
            result = pipeline.run()
        except Exception:
            logger.exception("Collection cycle failed before completion.")
            result = PipelineResult(failed=1)
            _log_pipeline_summary(result)
        _record_github_output(result)
        return int(result.failed > 0)

    # Continuous mode -------------------------------------------------------
    logger.info("Starting continuous mode (interval=%dh).", args.interval)
    sched = CollectionScheduler(
        collection_fn=pipeline.run,
        interval_hours=args.interval,
    )
    sched.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
