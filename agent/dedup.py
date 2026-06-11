"""Database-backed URL deduplication for SRE Atlas Agent.

Uses PostgreSQL via psycopg2.  The ``ingested_urls`` table is created
automatically on first use so the module is self-bootstrapping.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import psycopg2
from psycopg2 import OperationalError, extras

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------

_CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS ingested_urls (
    url         TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    category    TEXT,
    title       TEXT,
    ingested_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS wiki_pages (
    slug          TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    category      TEXT,
    confidence    TEXT CHECK (confidence IN ('high', 'medium', 'low')),
    source_url    TEXT,
    generated_at  TIMESTAMP DEFAULT NOW(),
    content_hash  TEXT
);

CREATE TABLE IF NOT EXISTS source_health (
    source           TEXT PRIMARY KEY,
    last_success     TIMESTAMP,
    last_error       TEXT,
    error_count      INTEGER DEFAULT 0,
    total_collected  INTEGER DEFAULT 0
);
"""


class DeduplicationError(Exception):
    """Raised when a database operation fails irrecoverably."""


class Dedup:
    """Track and filter already-ingested URLs via PostgreSQL.

    Parameters
    ----------
    dsn : str | None
        A libpq connection string.  Falls back to the ``DATABASE_URL``
        environment variable when *None*.
    """

    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn or os.environ.get("DATABASE_URL", "")
        if not self._dsn:
            raise DeduplicationError(
                "No database DSN supplied and DATABASE_URL is not set."
            )
        self._ensure_schema()

    # ------------------------------------------------------------------
    # Connection helper
    # ------------------------------------------------------------------

    def _connect(self):
        """Return a new ``psycopg2`` connection."""
        try:
            return psycopg2.connect(self._dsn)
        except OperationalError as exc:
            raise DeduplicationError(f"Failed to connect to database: {exc}") from exc

    def _ensure_schema(self) -> None:
        """Create required tables if they do not exist."""
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(_CREATE_TABLES_SQL)
            conn.commit()
            logger.info("Database schema verified / created.")
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_seen(self, url: str) -> bool:
        """Return *True* if *url* is already recorded in the database."""
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM ingested_urls WHERE url = %s LIMIT 1",
                    (url,),
                )
                return cur.fetchone() is not None
        finally:
            conn.close()

    def mark_seen(
        self,
        url: str,
        source: str,
        category: str,
        title: str,
    ) -> None:
        """Insert *url* into the database.  Duplicate URLs are silently ignored."""
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ingested_urls (url, source, category, title, ingested_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (url) DO NOTHING
                    """,
                    (url, source, category, title, datetime.now(timezone.utc)),
                )
            conn.commit()
            logger.debug("Marked seen: %s", url)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_stats(self) -> dict[str, dict[str, int]]:
        """Return ingestion counts grouped by source and category.

        Returns
        -------
        dict
            ``{"by_source": {"rss": 42, ...}, "by_category": {"kubernetes": 17, ...}}``
        """
        conn = self._connect()
        try:
            with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT source, COUNT(*) AS cnt FROM ingested_urls GROUP BY source"
                )
                by_source: dict[str, int] = {
                    row["source"]: row["cnt"] for row in cur.fetchall()
                }

                cur.execute(
                    "SELECT category, COUNT(*) AS cnt FROM ingested_urls GROUP BY category"
                )
                by_category: dict[str, int] = {
                    (row["category"] or "uncategorised"): row["cnt"]
                    for row in cur.fetchall()
                }

            return {"by_source": by_source, "by_category": by_category}
        finally:
            conn.close()
