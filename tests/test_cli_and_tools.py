from __future__ import annotations

import asyncio
import json

from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.registry import ToolRegistry

from nanobot_llm_wiki.cli import main
from nanobot_llm_wiki.diagnostics import diagnose_workspace
from nanobot_llm_wiki.installer import install_workspace
from nanobot_llm_wiki.storage import WikiStore
from nanobot_llm_wiki.tools import (
    WikiDoctorTool,
    WikiForgetTool,
    WikiImportTool,
    WikiLinkTool,
    WikiReadTool,
    WikiSearchTool,
    WikiUnlinkTool,
    WikiUpsertTool,
)


def test_cli_install_and_search(tmp_path, capsys) -> None:
    assert main(["--workspace", str(tmp_path), "install"]) == 0
    assert (tmp_path / "memory" / "wiki" / "wiki.db").exists()
    assert (tmp_path / "skills" / "llm-wiki" / "SKILL.md").exists()

    assert main(["--workspace", str(tmp_path), "search", "Projects"]) == 0
    out = capsys.readouterr().out
    assert "Projects" in out


def test_cli_uninstall_detaches_generated_files_and_keeps_data(tmp_path, capsys) -> None:
    memory_path = tmp_path / "memory" / "MEMORY.md"
    memory_path.parent.mkdir(parents=True)
    memory_path.write_text("User-owned memory.\n", encoding="utf-8")

    assert main(["--workspace", str(tmp_path), "install"]) == 0
    page_path = tmp_path / "memory" / "wiki" / "pages" / "projects.md"
    skill_path = tmp_path / "skills" / "llm-wiki" / "SKILL.md"
    assert page_path.exists()
    assert skill_path.exists()
    with memory_path.open("a", encoding="utf-8") as memory_file:
        memory_file.write("\nUser-owned trailing memory.\n")
    capsys.readouterr()

    assert main(["--workspace", str(tmp_path), "uninstall"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["memory_bridge_removed"] is True
    assert result["skill_removed"] is True
    assert "User-owned memory." in memory_path.read_text(encoding="utf-8")
    assert "User-owned trailing memory." in memory_path.read_text(encoding="utf-8")
    assert "nanobot-llm-wiki:start" not in memory_path.read_text(encoding="utf-8")
    assert not skill_path.exists()
    assert page_path.exists()


def test_cli_uninstall_preserves_a_user_owned_skill(tmp_path, capsys) -> None:
    assert main(["--workspace", str(tmp_path), "install"]) == 0
    skill_path = tmp_path / "skills" / "llm-wiki" / "SKILL.md"
    skill_path.write_text("# My custom Wiki workflow\n", encoding="utf-8")
    capsys.readouterr()

    assert main(["--workspace", str(tmp_path), "uninstall"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["skill_removed"] is False
    assert skill_path.read_text(encoding="utf-8") == "# My custom Wiki workflow\n"


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

    assert (
        main([
            "--workspace",
            str(tmp_path),
            "unlink",
            "User Profile",
            "Projects",
            "--relation",
            "uses",
        ])
        == 0
    )
    out = capsys.readouterr().out
    assert "Removed 1 link(s): User Profile -> Projects" in out
    assert WikiStore(tmp_path).list_links() == []


def test_cli_import_knowledge_base(tmp_path, capsys) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "guide.md").write_text("# Setup Guide\n\nInstall with one command.", encoding="utf-8")

    assert (
        main([
            "--workspace",
            str(tmp_path / "workspace"),
            "import",
            str(raw_dir),
            "--index-title",
            "Imported Docs",
            "--tag",
            "docs",
        ])
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert "raw_path" in payload
    assert "source_path" not in payload
    assert payload["index_page"]["title"] == "Imported Docs"
    assert payload["imported"][0]["title"] == "Setup Guide"
    assert payload["imported"][0]["raw_path"] == "guide.md"

    store = WikiStore(tmp_path / "workspace")
    assert store.search("one command")[0].page.title == "Setup Guide"
    assert store.list_links()[0].relation == "contains"


def test_cli_reindex_and_user_facing_errors(tmp_path, capsys) -> None:
    store = WikiStore(tmp_path)
    store.upsert_page(title="Manual Sync", content="Original content.")
    page_path = store.page_path("manual-sync")
    page_path.write_text(
        page_path.read_text(encoding="utf-8").replace("Original content.", "Edited on disk."),
        encoding="utf-8",
    )

    assert main(["--workspace", str(tmp_path), "reindex"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["indexed"] == 1
    assert WikiStore(tmp_path).search("Edited on disk")[0].page.title == "Manual Sync"

    assert main(["--workspace", str(tmp_path), "link", "missing", "Manual Sync"]) == 1
    captured = capsys.readouterr()
    assert "Error: page not found: missing" in captured.err
    assert "Traceback" not in captured.err


def test_cli_doctor_is_read_only_and_reports_installed_workspace(tmp_path, capsys) -> None:
    workspace = tmp_path / "missing-workspace"

    assert main(["--workspace", str(workspace), "doctor"]) == 1
    output = capsys.readouterr().out
    assert "doctor: unhealthy" in output
    assert "Workspace directory is missing" in output
    assert not workspace.exists()

    install_workspace(workspace)
    assert main(["--workspace", str(workspace), "doctor", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["health"] == "healthy"
    assert result["summary"]["errors"] == 0
    assert result["summary"]["warnings"] == 0


def test_doctor_reports_index_drift_without_changing_files(tmp_path) -> None:
    install_workspace(tmp_path)
    manual = tmp_path / "memory" / "wiki" / "pages" / "manual.md"
    manual.write_text("# Manual\n\nNot indexed yet.", encoding="utf-8")

    result = diagnose_workspace(tmp_path)

    assert result["health"] == "attention"
    index_check = next(check for check in result["checks"] if check["id"] == "index_sync")
    assert index_check["status"] == "warning"
    assert index_check["action"] == "reindex"
    assert WikiStore(tmp_path).get_page("Manual") is None


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


def test_tools_return_errors_for_bad_inputs(tmp_path) -> None:
    link_tool = WikiLinkTool(tmp_path)
    unlink_tool = WikiUnlinkTool(tmp_path)
    forget_tool = WikiForgetTool(tmp_path)
    import_tool = WikiImportTool(tmp_path)
    upsert_tool = WikiUpsertTool(tmp_path)

    assert asyncio.run(link_tool.execute("missing", "also missing")).startswith("Error: page not found")
    assert asyncio.run(unlink_tool.execute("missing", "also missing")).startswith("Error: page not found")
    assert asyncio.run(forget_tool.execute("missing")).startswith("Error: page not found")
    assert asyncio.run(import_tool.execute(str(tmp_path / "missing"))).startswith("Error: raw")
    assert asyncio.run(upsert_tool.execute(title="", content="empty title")).startswith("Error: title")


def test_doctor_tool_is_read_only(tmp_path) -> None:
    workspace = tmp_path / "missing"
    result = asyncio.run(WikiDoctorTool(workspace).execute())

    assert "doctor: unhealthy" in result
    assert "Workspace directory is missing" in result
    assert not workspace.exists()


def test_import_tool_round_trip(tmp_path) -> None:
    raw_dir = tmp_path / "tool-raw"
    raw_dir.mkdir()
    (raw_dir / "manual.md").write_text("# Tool Manual\n\nImported through wiki_import.", encoding="utf-8")

    import_tool = WikiImportTool(tmp_path / "workspace")
    result = asyncio.run(
        import_tool.execute(
            path=str(raw_dir),
            index_title="Tool Imported Docs",
            tags=["tool-docs"],
        )
    )
    assert "Imported knowledge base" in result
    assert "from raw" in result
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
        WikiDoctorTool,
        WikiUnlinkTool,
    ]).load(ctx, registry)

    assert {
        "wiki_search",
        "wiki_read",
        "wiki_upsert",
        "wiki_import",
        "wiki_doctor",
        "wiki_unlink",
    }.issubset(set(registered))
    assert registry.has("wiki_search")
    store = WikiStore(tmp_path)
    store.upsert_page(title="Loader Smoke", content="Tool loader can construct Wiki tools.")
    result = asyncio.run(registry.execute("wiki_search", {"query": "loader"}))
    assert "Loader Smoke" in result
