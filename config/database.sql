-- SRE Atlas Agent — PostgreSQL schema
-- Run once against the target database before first launch.

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
