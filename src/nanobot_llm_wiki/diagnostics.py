"""Read-only installation diagnostics for NanoBot LLM Wiki."""

from __future__ import annotations

import json
import sqlite3
from importlib.metadata import PackageNotFoundError, entry_points, version
from pathlib import Path
from typing import Any

from nanobot_llm_wiki.paths import default_workspace, expand_path
from nanobot_llm_wiki.storage import BRIDGE_END, BRIDGE_START

PACKAGE_NAME = "nanobot-llm-wiki"
EXPECTED_TOOL_ENTRY_POINTS = {
    "wiki_doctor",
    "wiki_forget",
    "wiki_import",
    "wiki_link",
    "wiki_read",
    "wiki_search",
    "wiki_status",
    "wiki_unlink",
    "wiki_upsert",
}
EXPECTED_TABLES = {"links", "page_fts", "pages"}


def _package_version() -> str:
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:
        return "unknown"


def diagnose_workspace(workspace: str | Path | None = None) -> dict[str, Any]:
    """Inspect a Wiki installation without creating or changing workspace files."""
    root = expand_path(workspace) if workspace else default_workspace()
    wiki_dir = root / "memory" / "wiki"
    pages_dir = wiki_dir / "pages"
    db_path = wiki_dir / "wiki.db"
    links_path = wiki_dir / "links.jsonl"
    memory_path = root / "memory" / "MEMORY.md"
    skill_path = root / "skills" / "llm-wiki" / "SKILL.md"
    config_path = wiki_dir / "config.toml"
    checks: list[dict[str, Any]] = []

    def add(
        check_id: str,
        label: str,
        status: str,
        message: str,
        *,
        path: Path | None = None,
        action: str | None = None,
    ) -> None:
        check: dict[str, Any] = {
            "id": check_id,
            "label": label,
            "status": status,
            "message": message,
        }
        if path is not None:
            check["path"] = str(path)
        if action:
            check["action"] = action
        checks.append(check)

    if root.is_dir():
        add("workspace", "Workspace", "ok", "Workspace directory is available.", path=root)
    elif root.exists():
        add("workspace", "Workspace", "error", "Workspace path is not a directory.", path=root)
    else:
        add("workspace", "Workspace", "error", "Workspace directory is missing.", path=root)

    if wiki_dir.is_dir():
        add("wiki_dir", "Wiki directory", "ok", "Wiki storage directory is available.", path=wiki_dir)
    else:
        add("wiki_dir", "Wiki directory", "error", "Wiki storage directory is missing.", path=wiki_dir)

    if pages_dir.is_dir():
        add("pages_dir", "Markdown pages", "ok", "Markdown page directory is available.", path=pages_dir)
    else:
        add("pages_dir", "Markdown pages", "error", "Markdown page directory is missing.", path=pages_dir)

    database_ids: set[str] | None = None
    database_links: set[tuple[str, str, str]] | None = None
    fts_count: int | None = None
    if not db_path.is_file():
        add("database", "SQLite database", "error", "Wiki database is missing.", path=db_path)
    else:
        try:
            uri = f"{db_path.resolve().as_uri()}?mode=ro"
            with sqlite3.connect(uri, uri=True) as conn:
                quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
                table_rows = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                ).fetchall()
                tables = {str(row[0]) for row in table_rows}
                missing_tables = sorted(EXPECTED_TABLES - tables)
                if "pages" in tables:
                    database_ids = {
                        str(row[0]) for row in conn.execute("SELECT id FROM pages").fetchall()
                    }
                if "page_fts" in tables:
                    fts_count = int(conn.execute("SELECT COUNT(*) FROM page_fts").fetchone()[0])
                if "links" in tables:
                    database_links = {
                        (str(row[0]), str(row[1]), str(row[2]))
                        for row in conn.execute(
                            "SELECT from_id, to_id, relation FROM links"
                        ).fetchall()
                    }
            if quick_check != "ok":
                add(
                    "database",
                    "SQLite database",
                    "error",
                    f"SQLite integrity check failed: {quick_check}",
                    path=db_path,
                )
            elif missing_tables:
                add(
                    "database",
                    "SQLite database",
                    "error",
                    f"Database schema is missing: {', '.join(missing_tables)}.",
                    path=db_path,
                )
            else:
                add(
                    "database",
                    "SQLite database",
                    "ok",
                    "Database integrity and schema checks passed.",
                    path=db_path,
                )
        except (OSError, sqlite3.Error) as exc:
            add(
                "database",
                "SQLite database",
                "error",
                f"Database could not be read: {exc}",
                path=db_path,
            )

    if pages_dir.is_dir() and database_ids is not None:
        markdown_ids = {
            path.stem
            for path in pages_dir.glob("*.md")
            if path.is_file() and not path.name.startswith(".")
        }
        unindexed = sorted(markdown_ids - database_ids)
        missing_files = sorted(database_ids - markdown_ids)
        fts_mismatch = fts_count is not None and fts_count != len(database_ids)
        details: list[str] = []
        if unindexed:
            details.append(f"{len(unindexed)} Markdown page(s) are not indexed")
        if missing_files:
            details.append(f"{len(missing_files)} database page(s) have no Markdown file")
        if fts_mismatch:
            details.append(
                f"full-text index has {fts_count} row(s) for {len(database_ids)} page(s)"
            )
        if details:
            add(
                "index_sync",
                "Search index",
                "warning",
                "; ".join(details) + ".",
                action="reindex",
            )
        else:
            add(
                "index_sync",
                "Search index",
                "ok",
                f"Markdown and search index agree on {len(database_ids)} page(s).",
            )

    if not links_path.is_file():
        add(
            "link_source",
            "Relationship source",
            "error",
            "Portable relationship source is missing.",
            path=links_path,
            action="install",
        )
    else:
        source_links: set[tuple[str, str, str]] = set()
        source_errors: list[str] = []
        try:
            lines = links_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            source_errors.append(str(exc))
        else:
            for line_number, line in enumerate(lines, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    source_errors.append(f"line {line_number}: {exc.msg}")
                    continue
                if not isinstance(record, dict):
                    source_errors.append(f"line {line_number}: expected an object")
                    continue
                from_id = str(record.get("from_id") or "").strip()
                to_id = str(record.get("to_id") or "").strip()
                relation = str(record.get("relation") or "related").strip() or "related"
                created_at = str(record.get("created_at") or "").strip()
                if not from_id or not to_id or not created_at:
                    source_errors.append(f"line {line_number}: required fields are missing")
                    continue
                source_links.add((from_id, to_id, relation))
        if source_errors:
            add(
                "link_source",
                "Relationship source",
                "error",
                "Relationship source is invalid: " + "; ".join(source_errors[:3]) + ".",
                path=links_path,
            )
        elif database_ids is not None and database_links is not None:
            active_source_links = {
                link
                for link in source_links
                if link[0] in database_ids and link[1] in database_ids
            }
            if active_source_links != database_links:
                add(
                    "link_source",
                    "Relationship source",
                    "warning",
                    "Relationship source and graph index are out of sync.",
                    path=links_path,
                    action="reindex",
                )
            else:
                add(
                    "link_source",
                    "Relationship source",
                    "ok",
                    f"Relationship source contains {len(source_links)} portable link(s).",
                    path=links_path,
                )
        else:
            add(
                "link_source",
                "Relationship source",
                "ok",
                f"Relationship source contains {len(source_links)} portable link(s).",
                path=links_path,
            )

    if memory_path.is_file():
        try:
            memory_text = memory_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            add(
                "memory_bridge",
                "Memory bridge",
                "error",
                f"Memory bridge could not be read: {exc}",
                path=memory_path,
            )
        else:
            if BRIDGE_START in memory_text and BRIDGE_END in memory_text:
                add(
                    "memory_bridge",
                    "Memory bridge",
                    "ok",
                    "NanoBot memory bridge is present.",
                    path=memory_path,
                )
            else:
                add(
                    "memory_bridge",
                    "Memory bridge",
                    "error",
                    "MEMORY.md does not contain the Wiki bridge block.",
                    path=memory_path,
                    action="install",
                )
    else:
        add(
            "memory_bridge",
            "Memory bridge",
            "error",
            "NanoBot memory bridge is missing.",
            path=memory_path,
            action="install",
        )

    if skill_path.is_file():
        try:
            skill_text = skill_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            add(
                "skill",
                "NanoBot skill",
                "error",
                f"Wiki skill could not be read: {exc}",
                path=skill_path,
            )
        else:
            required_names = {"wiki_read", "wiki_search", "wiki_upsert"}
            missing_names = sorted(name for name in required_names if name not in skill_text)
            if missing_names:
                add(
                    "skill",
                    "NanoBot skill",
                    "warning",
                    f"Wiki skill does not mention: {', '.join(missing_names)}.",
                    path=skill_path,
                    action="install",
                )
            else:
                add(
                    "skill",
                    "NanoBot skill",
                    "ok",
                    "Wiki skill exposes the core memory workflow.",
                    path=skill_path,
                )
    else:
        add(
            "skill",
            "NanoBot skill",
            "error",
            "Wiki skill is missing from the workspace.",
            path=skill_path,
            action="install",
        )

    if config_path.is_file():
        add("config", "Workspace config", "ok", "Workspace configuration is present.", path=config_path)
    else:
        add(
            "config",
            "Workspace config",
            "warning",
            "Workspace configuration is missing.",
            path=config_path,
            action="install",
        )

    registered = {
        item.name
        for item in entry_points(group="nanobot.tools")
        if item.value.startswith("nanobot_llm_wiki.tools:")
    }
    missing_tools = sorted(EXPECTED_TOOL_ENTRY_POINTS - registered)
    if missing_tools:
        add(
            "tool_registration",
            "NanoBot tools",
            "error",
            f"Tool entry points are missing: {', '.join(missing_tools)}.",
        )
    else:
        add(
            "tool_registration",
            "NanoBot tools",
            "ok",
            f"All {len(EXPECTED_TOOL_ENTRY_POINTS)} Wiki tools are registered.",
        )

    summary = {
        "passed": sum(check["status"] == "ok" for check in checks),
        "warnings": sum(check["status"] == "warning" for check in checks),
        "errors": sum(check["status"] == "error" for check in checks),
    }
    health = "unhealthy" if summary["errors"] else "attention" if summary["warnings"] else "healthy"
    return {
        "ok": summary["errors"] == 0,
        "health": health,
        "version": _package_version(),
        "workspace": str(root),
        "summary": summary,
        "checks": checks,
    }
