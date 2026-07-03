from __future__ import annotations

import asyncio
import json

from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.registry import ToolRegistry

from nanobot_llm_wiki.cli import main
from nanobot_llm_wiki.storage import WikiStore
from nanobot_llm_wiki.tools import WikiImportTool, WikiReadTool, WikiSearchTool, WikiUpsertTool


def test_cli_install_and_search(tmp_path, capsys) -> None:
    assert main(["--workspace", str(tmp_path), "install"]) == 0
    assert (tmp_path / "memory" / "wiki" / "wiki.db").exists()
    assert (tmp_path / "skills" / "llm-wiki" / "SKILL.md").exists()

    assert main(["--workspace", str(tmp_path), "search", "Projects"]) == 0
    out = capsys.readouterr().out
    assert "Projects" in out


def test_cli_link(tmp_path, capsys) -> None:
    assert main(["--workspace", str(tmp_path), "install"]) == 0
    assert (
        main([
            "--workspace",
            str(tmp_path),
            "link",
            "User Profile",
            "Projects",
            "--relation",
            "uses",
        ])
        == 0
    )
    out = capsys.readouterr().out
    assert "Linked User Profile -> Projects (uses)" in out
    assert WikiStore(tmp_path).list_links()[0].relation == "uses"


def test_cli_import_knowledge_base(tmp_path, capsys) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "guide.md").write_text("# Setup Guide\n\nInstall with one command.", encoding="utf-8")

    assert (
        main([
            "--workspace",
            str(tmp_path / "workspace"),
            "import",
            str(source),
            "--index-title",
            "Imported Docs",
            "--tag",
            "docs",
        ])
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["index_page"]["title"] == "Imported Docs"
    assert payload["imported"][0]["title"] == "Setup Guide"

    store = WikiStore(tmp_path / "workspace")
    assert store.search("one command")[0].page.title == "Setup Guide"
    assert store.list_links()[0].relation == "contains"


def test_tools_round_trip(tmp_path) -> None:
    upsert = WikiUpsertTool(tmp_path)
    search = WikiSearchTool(tmp_path)
    read = WikiReadTool(tmp_path)

    result = asyncio.run(
        upsert.execute(
            title="User Preference",
            content="The user prefers deployable tools with local tests.",
            tags=["user", "preference"],
        )
    )
    assert "Saved Wiki page" in result

    found = asyncio.run(search.execute(query="deployable tools", limit=3))
    assert "User Preference" in found

    content = asyncio.run(read.execute(selector="User Preference"))
    assert "local tests" in content


def test_import_tool_round_trip(tmp_path) -> None:
    source = tmp_path / "tool-source"
    source.mkdir()
    (source / "manual.md").write_text("# Tool Manual\n\nImported through wiki_import.", encoding="utf-8")

    import_tool = WikiImportTool(tmp_path / "workspace")
    result = asyncio.run(
        import_tool.execute(
            path=str(source),
            index_title="Tool Imported Docs",
            tags=["tool-docs"],
        )
    )
    assert "Imported knowledge base" in result
    assert "with 1 pages" in result

    store = WikiStore(tmp_path / "workspace")
    assert store.search("wiki_import")[0].page.title == "Tool Manual"
    assert store.list_links()[0].from_title == "Tool Imported Docs"


def test_nanobot_tool_loader_can_register_plugin_tools(tmp_path) -> None:
    ctx = ToolContext(config=None, workspace=str(tmp_path))
    registry = ToolRegistry()
    registered = ToolLoader(test_classes=[
        WikiSearchTool,
        WikiReadTool,
        WikiUpsertTool,
        WikiImportTool,
    ]).load(ctx, registry)

    assert {"wiki_search", "wiki_read", "wiki_upsert", "wiki_import"}.issubset(set(registered))
    assert registry.has("wiki_search")
    store = WikiStore(tmp_path)
    store.upsert_page(title="Loader Smoke", content="Tool loader can construct Wiki tools.")
    result = asyncio.run(registry.execute("wiki_search", {"query": "loader"}))
    assert "Loader Smoke" in result
