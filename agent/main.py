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

    # Preview without touching the database
    python -m agent.main --once --dry-run

    # Custom config / output / interval
    python -m agent.main --config config/sources.yaml --output output/ --interval 4
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

# Load .env before anything else reads env vars (GitHub collector etc.)
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import yaml

from agent.category_map import get_category  # noqa: F401
from agent.dedup import Dedup, DeduplicationError
from agent.scheduler import CollectionScheduler

# ---------------------------------------------------------------------------
# Heavy imports -- deferred so ``--help`` works without all deps installed.
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict[str, Any]:
    """Load and validate a ``sources.yaml`` file.

    Raises ``FileNotFoundError`` or ``ValueError`` on problems.
    """
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with config_path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    if not isinstance(data, dict):
        raise ValueError("Config must be a YAML mapping.")

    return data


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
        Directory where generated ``.md`` files are written.
    dry_run : bool
        When *True*, skip all database writes.
    dedup : Dedup | None
        Dedup instance.  Ignored when *dry_run* is *True*.
    """

    def __init__(
        self,
        config: dict[str, Any],
        config_path: str = "config/sources.yaml",
        output_dir: str = "output",
        dry_run: bool = False,
        dedup: Optional[Dedup] = None,
    ) -> None:
        self._config = config
        self._config_path = Path(config_path)
        self._output_dir = Path(output_dir)
        self._dry_run = dry_run
        self._dedup = dedup

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Execute the full pipeline."""
        from agent.collectors.rss_collector import RSSCollector, CollectedItem
        from agent.collectors.github_collector import GitHubCollector
        from agent.generator import ContentGenerator, GeneratedPage

        # 1. Collect -------------------------------------------------------
        logger.info("Step 1/5 -- Collecting items...")
        all_items: list[CollectedItem] = []

        if self._config.get("rss"):
            rss_collector = RSSCollector(config_path=self._config_path)
            all_items.extend(rss_collector.collect())

        if self._config.get("github"):
            gh_collector = GitHubCollector(config_path=self._config_path)
            all_items.extend(gh_collector.collect())

        logger.info("Collected %d item(s) total.", len(all_items))
        if not all_items:
            logger.info("Nothing collected -- cycle complete.")
            return

        # 2. Dedup ---------------------------------------------------------
        logger.info("Step 2/5 -- Deduplicating...")
        new_items: list[CollectedItem] = []
        seen_count = 0

        for item in all_items:
            if not item.url:
                logger.debug("Skipping item with empty URL: %s", item.title)
                continue
            if self._dedup and not self._dry_run and self._dedup.is_seen(item.url):
                seen_count += 1
                continue
            new_items.append(item)

        logger.info("%d new item(s), %d already seen.", len(new_items), seen_count)
        if not new_items:
            logger.info("No new items -- cycle complete.")
            return

        # 3. Generate + 4. Write (incremental) ------------------------------
        logger.info("Step 3/5 -- Generating wiki pages via Claude API...")
        generator = ContentGenerator()
        self._output_dir.mkdir(parents=True, exist_ok=True)
        total = len(new_items)
        written = 0
        generated_pages: list[GeneratedPage] = []

        for idx, item in enumerate(new_items, start=1):
            logger.info("[%d/%d] Generating page for: %s", idx, total, item.title)
            try:
                page = generator.generate_page(item)
            except Exception:
                logger.exception("Unhandled error generating page for %r", item.title)
                page = None

            if page is not None:
                category_dir = self._output_dir / page.category
                category_dir.mkdir(parents=True, exist_ok=True)
                out_file = category_dir / f"{page.slug}.mdx"
                out_file.write_text(page.content, encoding="utf-8")
                written += 1
                generated_pages.append(page)
                logger.info("[%d/%d] OK -- slug=%s confidence=%s  (wrote %s)", idx, total, page.slug, page.confidence, out_file)
            else:
                logger.info("[%d/%d] SKIPPED: %s", idx, total, item.title)

            # Rate limiting
            import time; time.sleep(1.2)

            # 每50条保存一次进度
            if len(generated_pages) % 50 == 0 and generated_pages:
                logger.info("Step 5/5 -- Saving progress (%d pages so far)...", len(generated_pages))
                if self._dedup and not self._dry_run:
                    item_by_url_interim = {item.url: item for item in new_items}
                    for page_batch in generated_pages[-50:]:
                        source_item = item_by_url_interim.get(page_batch.source_url)
                        try:
                            self._dedup.mark_seen(
                                url=page_batch.source_url,
                                source=source_item.source if source_item else "unknown",
                                category=page_batch.category,
                                title=page_batch.title,
                            )
                        except Exception:
                            logger.exception("Failed to mark seen: %s", page_batch.source_url)
                    logger.info("Saved progress: %d pages", len(generated_pages))

        logger.info("Generated %d page(s), wrote %d.", total, written)

        # 5. Mark seen -----------------------------------------------------
        logger.info("Step 5/5 -- Marking URLs as seen...")
        if self._dedup and not self._dry_run:
            # Build a lookup from url -> item for metadata.
            item_by_url: dict[str, CollectedItem] = {
                item.url: item for item in new_items
            }
            for page in generated_pages:
                source_item = item_by_url.get(page.source_url)
                try:
                    self._dedup.mark_seen(
                        url=page.source_url,
                        source=source_item.source if source_item else "unknown",
                        category=page.category,
                        title=page.title,
                    )
                except Exception:
                    logger.exception("Failed to mark seen: %s", page.source_url)
        elif self._dry_run:
            logger.info("[dry-run] Skipping database writes.")

        logger.info("Cycle complete -- %d new page(s) written.", len(generated_pages))


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
        help="Collect and generate but do not write to the database.",
    )
    parser.add_argument(
        "--config",
        default="config/sources.yaml",
        help="Path to the sources configuration file (default: config/sources.yaml).",
    )
    parser.add_argument(
        "--output",
        default="output",
        help="Directory for generated wiki pages (default: output/).",
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
    except ValueError as exc:
        logger.error("Invalid config: %s", exc)
        return 1

    # Initialise dedup ------------------------------------------------------
    dedup: Optional[Dedup] = None
    if not args.dry_run:
        try:
            dedup = Dedup()
        except DeduplicationError as exc:
            logger.error("Dedup init failed: %s", exc)
            return 1
    else:
        logger.info("[dry-run] Database connection skipped.")

    # Build pipeline --------------------------------------------------------
    pipeline = AtlasPipeline(
        config=config,
        config_path=args.config,
        output_dir=args.output,
        dry_run=args.dry_run,
        dedup=dedup,
    )

    if args.once:
        pipeline.run()
        return 0

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
