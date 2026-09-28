"""Database-backed URL deduplication for SRE Atlas Agent.

Uses SQLite via the standard-library ``sqlite3`` module.  Tables are
created automatically on first use so the module is self-bootstrapping.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

logger = logging.getLogger(__name__)

_CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS ingested_urls (
    url         TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    category    TEXT,
    title       TEXT,
    ingested_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS wiki_pages (
    slug          TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    category      TEXT,
    confidence    TEXT CHECK (confidence IN ('high', 'medium', 'low')),
    source_url    TEXT,
    generated_at  TEXT DEFAULT (datetime('now')),
    content_hash  TEXT
);

CREATE TABLE IF NOT EXISTS generated_results (
    source_url    TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    slug          TEXT NOT NULL,
    title         TEXT NOT NULL,
    category      TEXT NOT NULL,
    confidence    TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
    content       TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    generated_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS source_health (
    source           TEXT PRIMARY KEY,
    last_success     TEXT,
    last_error       TEXT,
    error_count      INTEGER DEFAULT 0,
    total_collected  INTEGER DEFAULT 0
);
"""

_PRAGMAS = "PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;"


class DeduplicationError(Exception):
    """Raised when a database operation fails irrecoverably."""


class Dedup:
    """Track and filter already-ingested URLs via SQLite.

    Parameters
    ----------
    db_path : str | None
        Path to the SQLite database file.  Falls back to the
        ``DATABASE_PATH`` environment variable when *None*, and then to
        ``data/sre_atlas.db``.
    """

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or os.environ.get("DATABASE_PATH", "data/sre_atlas.db")
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        """Return a new connection with WAL mode and dict-like row access."""
        try:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            conn.executescript(_PRAGMAS)
            return conn
        except sqlite3.Error as exc:
            raise DeduplicationError(f"Failed to connect to database: {exc}") from exc

    def _ensure_schema(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(_CREATE_TABLES_SQL)
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
            cur = conn.execute(
                "SELECT 1 FROM ingested_urls WHERE url = ? LIMIT 1",
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
            conn.execute(
                """
                INSERT OR IGNORE INTO ingested_urls (url, source, category, title, ingested_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (url, source, category, title, datetime.now(UTC).isoformat()),
            )
            conn.execute("DELETE FROM generated_results WHERE source_url = ?", (url,))
            conn.commit()
            logger.debug("Marked seen: %s", url)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_generated_result(self, source_url: str) -> dict[str, str] | None:
        """Return a durable generated page that has not yet been marked seen."""
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT source_url, source, slug, title, category, confidence,
                       content, content_hash
                FROM generated_results WHERE source_url = ?
                """,
                (source_url,),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            actual_hash = sha256(result["content"].encode("utf-8")).hexdigest()
            if actual_hash != result["content_hash"]:
                raise DeduplicationError(
                    f"Generated result checksum mismatch for {source_url}"
                )
            return result
        finally:
            conn.close()

    def persist_generated_result(
        self,
        *,
        source_url: str,
        source: str,
        slug: str,
        title: str,
        category: str,
        confidence: str,
        content: str,
    ) -> dict[str, str]:
        """Persist a successful generation before writing its MDX file.

        On a duplicate key, return the previously stored generation so a
        concurrent or retried run reuses the first paid result.
        """
        content_hash = sha256(content.encode("utf-8")).hexdigest()
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO generated_results
                    (source_url, source, slug, title, category, confidence, content, content_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_url,
                    source,
                    slug,
                    title,
                    category,
                    confidence,
                    content,
                    content_hash,
                ),
            )
            row = conn.execute(
                """
                SELECT source_url, source, slug, title, category, confidence,
                       content, content_hash
                FROM generated_results WHERE source_url = ?
                """,
                (source_url,),
            ).fetchone()
            conn.commit()
            if row is None:
                raise DeduplicationError(
                    f"Failed to persist generated result for {source_url}"
                )
            result = dict(row)
            if (
                sha256(result["content"].encode("utf-8")).hexdigest()
                != result["content_hash"]
            ):
                raise DeduplicationError(
                    f"Generated result checksum mismatch for {source_url}"
                )
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_stats(self) -> dict[str, dict[str, int]]:
        """Return ingestion counts grouped by source and category."""
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT source, COUNT(*) AS cnt FROM ingested_urls GROUP BY source"
            )
            by_source: dict[str, int] = {
                row["source"]: row["cnt"] for row in cur.fetchall()
            }

            cur = conn.execute(
                "SELECT category, COUNT(*) AS cnt FROM ingested_urls GROUP BY category"
            )
            by_category: dict[str, int] = {
                (row["category"] or "uncategorised"): row["cnt"]
                for row in cur.fetchall()
            }

            return {"by_source": by_source, "by_category": by_category}
        finally:
            conn.close()
