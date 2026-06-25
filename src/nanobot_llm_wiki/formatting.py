"""Human-readable formatting helpers."""

from __future__ import annotations

import json
from typing import Any

from nanobot_llm_wiki.storage import SearchResult, WikiPage


def page_summary(page: WikiPage) -> str:
    preview = " ".join(page.content.split())
    if len(preview) > 240:
        preview = preview[:239].rstrip() + "…"
    tags = f" tags={page.tags}" if page.tags else ""
    return f"- {page.title} (`{page.id}`){tags}: {preview}"


def format_search_results(results: list[SearchResult]) -> str:
    if not results:
        return "No Wiki pages matched."
    return "\n".join(page_summary(result.page) for result in results)


def format_page(page: WikiPage) -> str:
    meta = {
        "id": page.id,
        "title": page.title,
        "type": page.page_type,
        "tags": page.tags,
        "aliases": page.aliases,
        "confidence": page.confidence,
        "source_cursors": page.source_cursors,
        "updated_at": page.updated_at,
    }
    return f"```json\n{json.dumps(meta, ensure_ascii=False, indent=2)}\n```\n\n{page.content}"


def format_status(status: dict[str, Any]) -> str:
    return "\n".join(f"{key}: {value}" for key, value in status.items())
