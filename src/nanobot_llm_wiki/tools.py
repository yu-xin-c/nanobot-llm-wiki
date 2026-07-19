"""NanoBot tool entry points for LLM Wiki."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.context import ToolContext

from nanobot_llm_wiki.diagnostics import diagnose_workspace
from nanobot_llm_wiki.formatting import (
    format_doctor,
    format_page,
    format_search_results,
    format_status,
)
from nanobot_llm_wiki.paths import default_workspace, expand_path
from nanobot_llm_wiki.storage import WikiStore


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, KeyError) and exc.args:
        return str(exc.args[0])
    return str(exc)


class _WikiTool(Tool):
    _scopes = {"core", "subagent"}

    def __init__(self, workspace: str | Path | None = None):
        self.store = WikiStore(workspace)

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls(workspace=ctx.workspace)


class WikiSearchTool(_WikiTool):
    @property
    def name(self) -> str:
        return "wiki_search"

    @property
    def description(self) -> str:
        return "Search NanoBot's local LLM Wiki long-term memory."

    @property
    def read_only(self) -> bool:
        return True

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25, "default": 5},
                "tag": {"type": ["string", "null"], "description": "Optional exact tag filter."},
            },
            "required": ["query"],
        }

    async def execute(self, query: str, limit: int = 5, tag: str | None = None) -> str:
        return format_search_results(self.store.search(query, limit=limit, tag=tag))


class WikiReadTool(_WikiTool):
    @property
    def name(self) -> str:
        return "wiki_read"

    @property
    def description(self) -> str:
        return "Read one NanoBot LLM Wiki page by title, id, or alias."

    @property
    def read_only(self) -> bool:
        return True

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "Page title, id, or alias."},
            },
            "required": ["selector"],
        }

    async def execute(self, selector: str) -> str:
        page = self.store.get_page(selector)
        if not page:
            return f"Error: Wiki page not found: {selector}"
        return format_page(page)


class WikiUpsertTool(_WikiTool):
    @property
    def name(self) -> str:
        return "wiki_upsert"

    @property
    def description(self) -> str:
        return "Create, replace, or append to a NanoBot LLM Wiki page."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Page title."},
                "content": {"type": "string", "description": "Markdown content to write."},
                "page_type": {"type": "string", "default": "note"},
                "tags": {"type": "array", "items": {"type": "string"}, "default": []},
                "aliases": {"type": "array", "items": {"type": "string"}, "default": []},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.7},
                "source_cursors": {"type": "array", "items": {"type": "integer"}, "default": []},
                "mode": {"type": "string", "enum": ["replace", "append"], "default": "replace"},
            },
            "required": ["title", "content"],
        }

    async def execute(
        self,
        title: str,
        content: str,
        page_type: str = "note",
        tags: list[str] | None = None,
        aliases: list[str] | None = None,
        confidence: float = 0.7,
        source_cursors: list[int] | None = None,
        mode: str = "replace",
    ) -> str:
        try:
            page = self.store.upsert_page(
                title=title,
                content=content,
                page_type=page_type,
                tags=tags or [],
                aliases=aliases or [],
                confidence=confidence,
                source_cursors=source_cursors or [],
                mode=mode,
            )
        except ValueError as exc:
            return f"Error: {_error_text(exc)}"
        self.store.write_memory_bridge()
        return f"Saved Wiki page `{page.title}` (`{page.id}`) with {len(page.content)} characters."


class WikiLinkTool(_WikiTool):
    @property
    def name(self) -> str:
        return "wiki_link"

    @property
    def description(self) -> str:
        return "Create a typed link between two NanoBot LLM Wiki pages."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "from_selector": {"type": "string"},
                "to_selector": {"type": "string"},
                "relation": {"type": "string", "default": "related"},
            },
            "required": ["from_selector", "to_selector"],
        }

    async def execute(self, from_selector: str, to_selector: str, relation: str = "related") -> str:
        try:
            from_page, to_page = self.store.link_pages(from_selector, to_selector, relation)
        except (KeyError, ValueError) as exc:
            return f"Error: {_error_text(exc)}"
        return f"Linked `{from_page.title}` -> `{to_page.title}` as `{relation or 'related'}`."


class WikiUnlinkTool(_WikiTool):
    @property
    def name(self) -> str:
        return "wiki_unlink"

    @property
    def description(self) -> str:
        return "Remove one or all typed links between two NanoBot LLM Wiki pages."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "from_selector": {"type": "string"},
                "to_selector": {"type": "string"},
                "relation": {
                    "type": ["string", "null"],
                    "description": "Relation to remove; omit it to remove every directed link.",
                },
            },
            "required": ["from_selector", "to_selector"],
        }

    async def execute(
        self,
        from_selector: str,
        to_selector: str,
        relation: str | None = None,
    ) -> str:
        try:
            from_page, to_page, removed = self.store.unlink_pages(
                from_selector,
                to_selector,
                relation,
            )
        except (KeyError, ValueError) as exc:
            return f"Error: {_error_text(exc)}"
        scope = f" as `{relation}`" if relation else ""
        return (
            f"Removed {removed} link(s) from `{from_page.title}` "
            f"to `{to_page.title}`{scope}."
        )


class WikiForgetTool(_WikiTool):
    @property
    def name(self) -> str:
        return "wiki_forget"

    @property
    def description(self) -> str:
        return "Delete or archive a NanoBot LLM Wiki page when the user asks to forget it."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "Page title, id, or alias."},
                "archive": {"type": "boolean", "default": True},
            },
            "required": ["selector"],
        }

    async def execute(self, selector: str, archive: bool = True) -> str:
        try:
            page = self.store.forget_page(selector, archive=archive)
        except KeyError as exc:
            return f"Error: {_error_text(exc)}"
        self.store.write_memory_bridge()
        action = "Archived" if archive else "Deleted"
        return f"{action} Wiki page `{page.title}` (`{page.id}`)."


class WikiImportTool(_WikiTool):
    @property
    def name(self) -> str:
        return "wiki_import"

    @property
    def description(self) -> str:
        return "Import a local raw text knowledge base into NanoBot LLM Wiki pages."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Local raw file or directory path to import.",
                },
                "index_title": {
                    "type": ["string", "null"],
                    "description": "Optional title for the generated index page.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                    "description": "Extra tags for the index page and imported pages.",
                },
                "page_type": {"type": "string", "default": "knowledge-doc"},
                "relation": {
                    "type": "string",
                    "default": "contains",
                    "description": "Graph relation from index page to imported pages.",
                },
                "max_bytes": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 512000,
                    "description": "Maximum bytes per imported file.",
                },
            },
            "required": ["path"],
        }

    async def execute(
        self,
        path: str,
        index_title: str | None = None,
        tags: list[str] | None = None,
        page_type: str = "knowledge-doc",
        relation: str = "contains",
        max_bytes: int = 512_000,
    ) -> str:
        try:
            result = self.store.import_knowledge_base(
                path,
                index_title=index_title,
                tags=tags or [],
                page_type=page_type,
                relation=relation,
                max_bytes=max_bytes,
            )
        except (FileNotFoundError, OSError, ValueError) as exc:
            return f"Error: {_error_text(exc)}"
        skipped = f", skipped {len(result.skipped)} files" if result.skipped else ""
        return (
            f"Imported knowledge base `{result.index_page.title}` (`{result.index_page.id}`) "
            f"from raw `{result.raw_path}` with {len(result.imported)} pages{skipped}."
        )


class WikiStatusTool(_WikiTool):
    @property
    def name(self) -> str:
        return "wiki_status"

    @property
    def description(self) -> str:
        return "Show NanoBot LLM Wiki storage status."

    @property
    def read_only(self) -> bool:
        return True

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self) -> str:
        return format_status(self.store.status())


class WikiDoctorTool(Tool):
    _scopes = {"core", "subagent"}

    def __init__(self, workspace: str | Path | None = None):
        self.workspace = expand_path(workspace) if workspace else default_workspace()

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls(workspace=ctx.workspace)

    @property
    def name(self) -> str:
        return "wiki_doctor"

    @property
    def description(self) -> str:
        return "Run read-only health checks for the NanoBot LLM Wiki installation and search index."

    @property
    def read_only(self) -> bool:
        return True

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self) -> str:
        return format_doctor(diagnose_workspace(self.workspace))
