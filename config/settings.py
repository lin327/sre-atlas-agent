import os
from pathlib import Path

from dotenv import load_dotenv

# Paths
PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"

# Load .env from project root
load_dotenv(PROJECT_ROOT / ".env")

# Database
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/sre_atlas.db")

# Claude API
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "")
CLAUDE_MODEL = "mimo-v2.5"

# Collection
MAX_ITEMS_PER_SOURCE = 20
COLLECTION_INTERVAL_HOURS = 6

# Quality
MIN_CONFIDENCE = "medium"
MIN_CONTENT_LENGTH = 200
