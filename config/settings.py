import os
from pathlib import Path

# Paths
PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"

# Database
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/sre_atlas.db")

# Claude API
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")


def get_claude_model() -> str:
    """Return the configured model, honoring runtime environment overrides."""
    return os.getenv("CLAUDE_MODEL", CLAUDE_MODEL)


# Collection
MAX_ITEMS_PER_SOURCE = 5
MAX_PAGES_PER_RUN = 10
MAX_INPUT_CHARS = 20_000
COLLECTION_INTERVAL_HOURS = 6

# Quality
MIN_CONFIDENCE = "medium"
MIN_CONTENT_LENGTH = 200
