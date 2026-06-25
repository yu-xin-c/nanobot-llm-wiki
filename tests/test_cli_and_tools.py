from __future__ import annotations

import asyncio

from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.registry import ToolRegistry

from nanobot_llm_wiki.cli import main
from nanobot_llm_wiki.storage import WikiStore
from nanobot_llm_wiki.tools import WikiReadTool, WikiSearchTool, WikiUpsertTool


def test_cli_install_and_search(tmp_path, capsys) -> None:
    assert main(["--workspace", str(tmp_path), "install"]) == 0
    assert (tmp_path / "memory" / "wiki" / "wiki.db").exists()
    assert (tmp_path / "skills" / "llm-wiki" / "SKILL.md").exists()

    assert main(["--workspace", str(tmp_path), "search", "Projects"]) == 0
    out = capsys.readouterr().out
    assert "Projects" in out


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


def test_nanobot_tool_loader_can_register_plugin_tools(tmp_path) -> None:
    ctx = ToolContext(config=None, workspace=str(tmp_path))
    registry = ToolRegistry()
    registered = ToolLoader(test_classes=[WikiSearchTool, WikiReadTool, WikiUpsertTool]).load(ctx, registry)

    assert {"wiki_search", "wiki_read", "wiki_upsert"}.issubset(set(registered))
    assert registry.has("wiki_search")
    store = WikiStore(tmp_path)
    store.upsert_page(title="Loader Smoke", content="Tool loader can construct Wiki tools.")
    result = asyncio.run(registry.execute("wiki_search", {"query": "loader"}))
    assert "Loader Smoke" in result
