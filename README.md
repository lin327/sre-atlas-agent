# SRE Atlas Agent

Automated content collection agent that aggregates SRE and infrastructure knowledge from RSS feeds, GitHub repositories, and documentation sites. Collected content is analyzed with Claude and stored in PostgreSQL for downstream consumption.

## Project Structure

```
sre-atlas-agent/
├── config/
│   ├── sources.yaml      # Data source definitions (RSS, GitHub, docs)
│   └── settings.py       # Application settings and constants
├── .env.example           # Environment variable template
├── requirements.txt       # Python dependencies
└── README.md
```

## Setup

### 1. Create a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment

```bash
cp .env.example .env
# Edit .env with your actual credentials
```

Required environment variables:

| Variable | Description |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string |
| `ANTHROPIC_API_KEY` | Claude API key for content analysis |
| `GITHUB_TOKEN` | GitHub personal access token for API access |

### 4. Set up the database

Create the target database:

```bash
createdb sre_atlas
```

### 5. Configure sources

Edit `config/sources.yaml` to add, remove, or modify data sources. Each source type supports multiple entries:

- **rss** -- RSS/Atom feed URLs with category tags
- **github** -- Repositories filtered by issue labels
- **docs** -- Documentation site base URLs for crawling

## Usage

Run the collection agent:

```bash
python -m src.collector
```

The agent will:
1. Read source definitions from `config/sources.yaml`
2. Fetch content from each source
3. Analyze and classify content with Claude
4. Store results in PostgreSQL

## Configuration

Key settings in `config/settings.py`:

| Setting | Default | Description |
|---|---|---|
| `MAX_ITEMS_PER_SOURCE` | 20 | Maximum items fetched per source per run |
| `COLLECTION_INTERVAL_HOURS` | 6 | Hours between collection runs |
| `MIN_CONFIDENCE` | `"medium"` | Minimum confidence threshold for stored content |
| `MIN_CONTENT_LENGTH` | 200 | Minimum character length to keep an item |
| `CLAUDE_MODEL` | `claude-sonnet-4-6` | Claude model used for analysis |

## License

MIT
