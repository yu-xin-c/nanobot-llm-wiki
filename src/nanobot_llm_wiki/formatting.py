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


def format_doctor(result: dict[str, Any]) -> str:
    """Format structured diagnostics for a terminal or NanoBot tool response."""
    labels = {"ok": "OK", "warning": "WARN", "error": "ERROR"}
    lines = [
        f"NanoBot LLM Wiki doctor: {result['health']} (version {result['version']})",
        f"Workspace: {result['workspace']}",
        "",
    ]
    for check in result["checks"]:
        line = f"[{labels[check['status']]}] {check['label']}: {check['message']}"
        if check.get("action") == "reindex":
            line += " Fix: nanobot-wiki reindex"
        elif check.get("action") == "install":
            line += " Fix: nanobot-wiki install"
        lines.append(line)
    summary = result["summary"]
    lines.extend(
        [
            "",
            (
                f"Summary: {summary['passed']} passed, {summary['warnings']} warning(s), "
                f"{summary['errors']} error(s)"
            ),
        ]
    )
    return "\n".join(lines)
