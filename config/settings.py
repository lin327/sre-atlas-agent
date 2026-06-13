import os
from pathlib import Path

# Paths
PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"

# Database
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/sre_atlas.db")

# Claude API
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-sonnet-4-6"

# Collection
MAX_ITEMS_PER_SOURCE = 20
COLLECTION_INTERVAL_HOURS = 6

# Quality
MIN_CONFIDENCE = "medium"
MIN_CONTENT_LENGTH = 200
