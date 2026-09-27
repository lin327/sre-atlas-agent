"""Normalize model metadata and validate the shared Wiki frontmatter contract."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urlsplit

SCHEMA = json.loads(
    (Path(__file__).resolve().parents[1] / "config/frontmatter.schema.json").read_text()
)


def _http_url(value: object) -> bool:
    if (not isinstance(value, str) or not re.match(r"https?://", value, re.IGNORECASE)
            or re.search(r"\s|[\x00-\x1f\x7f]", value)):
        return False
    try:
        parsed = urlsplit(value)
        _ = parsed.port  # Reject malformed or out-of-range ports as well as hosts.
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname)
    except ValueError:
        return False


def _date_string(value: object) -> str | None:
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            pass
    return None


def _validate(value: object, schema: dict, path: str) -> None:
    expected = {"object": dict, "array": list, "string": str, "boolean": bool}[schema["type"]]
    if not isinstance(value, expected):
        raise ValueError(f"{path}: expected {schema['type']}")  # noqa: TRY004 - schema violation
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path}: invalid enum value")
    if isinstance(value, dict):
        if any(key not in value for key in schema.get("required", [])):
            raise ValueError(f"{path}: missing required field")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False and value.keys() - properties.keys():
            raise ValueError(f"{path}: unknown field")
        for key, item in value.items():
            if key in properties:
                _validate(item, properties[key], f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate(item, schema["items"], f"{path}[{index}]")
    elif isinstance(value, str):
        if len(value.strip()) < schema.get("minLength", 0):
            raise ValueError(f"{path}: empty string")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            raise ValueError(f"{path}: invalid pattern")
        if schema.get("format") == "date" and _date_string(value) is None:
            raise ValueError(f"{path}: invalid date")
        if schema.get("format") == "uri" and not _http_url(value):
            raise ValueError(f"{path}: expected HTTP(S) URL")


def validate_frontmatter(metadata: dict, slug: str) -> None:
    """Validate the contract's supported JSON Schema keywords; fail on drift."""
    _validate(metadata, SCHEMA, "frontmatter")
    _validate(slug, SCHEMA["$defs"]["slug"], "slug")


def normalize_frontmatter(metadata: dict, *, item, category: str) -> dict:
    title = metadata.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("Frontmatter title must be a nonempty string")
    today = datetime.now(UTC).date().isoformat()
    updated = _date_string(metadata.get("updated")) or _date_string(metadata.get("lastUpdated")) or today
    confidence = str(metadata.get("confidence", "medium")).lower()
    page_type = metadata.get("type")
    if page_type not in SCHEMA["properties"]["type"]["enum"]:
        page_type = {
            "runbooks": "runbook", "architectures": "architecture",
            "incidents": "incident", "comparisons": "comparison",
        }.get(category, "concept")

    # Preserve collected provenance even when the model invents or omits sources.
    sources = [{"url": item.url, "title": item.title.strip()}]
    seen = {item.url}
    raw_sources = metadata.get("sources", [])
    for source in raw_sources if isinstance(raw_sources, list) else []:
        source = {"url": source, "title": source} if isinstance(source, str) else source
        if not isinstance(source, dict):
            continue
        url = source.get("url")
        url = url.strip() if isinstance(url, str) else url
        if not _http_url(url) or url in seen:
            continue
        source_title = source.get("title")
        source_title = source_title.strip() if isinstance(source_title, str) else ""
        sources.append({"url": url, "title": source_title or url})
        seen.add(url)

    raw_tags = metadata.get("tags")
    raw_tags = raw_tags if isinstance(raw_tags, list) else item.tags
    normalized = {
        "title": title.strip(),
        "created": _date_string(metadata.get("created")) or updated,
        "updated": updated,
        "sources": sources,
        "canonical": False,
        "category": category,
        "type": page_type,
        "confidence": confidence if confidence in {"high", "medium", "low"} else "low",
        "tags": list(dict.fromkeys(tag.strip() for tag in raw_tags
                                   if isinstance(tag, str) and tag.strip())),
    }
    if isinstance(metadata.get("description"), str):
        normalized["description"] = metadata["description"].strip()
    return normalized
