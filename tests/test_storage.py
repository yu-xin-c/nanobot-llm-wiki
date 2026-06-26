from __future__ import annotations

import json

from nanobot_llm_wiki.storage import BRIDGE_START, WikiStore, slugify


def test_slugify_keeps_useful_unicode() -> None:
    assert slugify("NanoBot LLM Wiki") == "nanobot-llm-wiki"
    assert slugify("长期记忆 Wiki") == "长期记忆-wiki"


def test_upsert_search_read_and_forget(tmp_path) -> None:
    store = WikiStore(tmp_path)
    page = store.upsert_page(
        title="Project Memory",
        content="NanoBot should remember project decisions in a local wiki.",
        tags=["project", "memory"],
        aliases=["memory project"],
        source_cursors=[1, 2],
    )

    assert page.id == "project-memory"
    assert store.get_page("memory project").id == page.id
    assert store.search("project decisions")[0].page.id == page.id
    assert store.page_path(page.id).exists()

    updated = store.upsert_page(
        title="Project Memory",
        content="New durable preference.",
        tags=["preference"],
        source_cursors=[3],
        mode="append",
    )
    assert "New durable preference" in updated.content
    assert updated.source_cursors == [1, 2, 3]
    assert "preference" in updated.tags

    forgotten = store.forget_page("Project Memory")
    assert forgotten.id == page.id
    assert store.get_page(page.id) is None
    assert list(store.archive_dir.glob("project-memory-*.md"))


def test_recall_uses_aliases_precise_tokens_and_deletion_cleanup(tmp_path) -> None:
    store = WikiStore(tmp_path)
    store.upsert_page(
        title="General Codex Notes",
        content="Codex is used for NanoBot plugin development.",
        tags=["codex"],
    )
    target = store.upsert_page(
        title="Conversation Memory Probe",
        content="A temporary memory with exact token codex-dialogue-memory-43827.",
        tags=["codex-test", "delete-test"],
        aliases=["dialogue recall probe"],
    )

    assert store.search("dialogue recall probe")[0].page.id == target.id
    assert store.search("codex-dialogue-memory-43827")[0].page.id == target.id
    assert store.search("memory", tag="delete-test")[0].page.id == target.id

    forgotten = store.forget_page("dialogue recall probe", archive=False)
    assert forgotten.id == target.id
    assert store.search("codex-dialogue-memory-43827") == []
    assert store.search("memory", tag="delete-test") == []
    assert store.get_page("dialogue recall probe") is None


def test_history_ingestion_and_bridge(tmp_path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    history = memory_dir / "history.jsonl"
    history.write_text(
        "\n".join(
            [
                json.dumps({"cursor": 1, "timestamp": "2026-06-25 10:00", "content": "User likes compact UIs."}),
                json.dumps({"cursor": 2, "timestamp": "2026-06-25 10:05", "content": "Project: build LLM Wiki plugin."}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    store = WikiStore(tmp_path)
    result = store.ingest_history()
    assert result["processed"] == 2
    assert result["cursor"] == 2

    inbox = store.get_page("Conversation Inbox")
    assert inbox is not None
    assert "User likes compact UIs" in inbox.content

    memory_path = store.write_memory_bridge()
    assert BRIDGE_START in memory_path.read_text(encoding="utf-8")


def test_page_links_and_graph(tmp_path) -> None:
    store = WikiStore(tmp_path)
    store.upsert_page(title="User Profile", content="User facts.")
    store.upsert_page(title="NanoBot Project", content="Project facts.")

    from_page, to_page = store.link_pages("User Profile", "NanoBot Project", "working_on")
    assert from_page.id == "user-profile"
    assert to_page.id == "nanobot-project"

    links = store.list_links()
    assert len(links) == 1
    assert links[0].relation == "working_on"

    graph = store.graph()
    assert {node["id"] for node in graph["nodes"]} == {"user-profile", "nanobot-project"}
    assert graph["links"] == [
        {
            "from_id": "user-profile",
            "from_title": "User Profile",
            "to_id": "nanobot-project",
            "to_title": "NanoBot Project",
            "relation": "working_on",
            "created_at": links[0].created_at,
        }
    ]


def test_skill_writer_does_not_overwrite_user_skill(tmp_path) -> None:
    store = WikiStore(tmp_path)
    skill_path = tmp_path / "skills" / "llm-wiki" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("custom", encoding="utf-8")

    store.write_skill()
    assert skill_path.read_text(encoding="utf-8") == "custom"

    store.write_skill(force=True)
    assert "wiki_search" in skill_path.read_text(encoding="utf-8")


def test_memory_bridge_includes_recent_graph_links(tmp_path) -> None:
    store = WikiStore(tmp_path)
    store.upsert_page(title="User Profile", content="User facts.")
    store.upsert_page(title="NanoBot Project", content="Project facts.")
    store.link_pages("User Profile", "NanoBot Project", "working_on")

    memory_path = store.write_memory_bridge()
    text = memory_path.read_text(encoding="utf-8")

    assert "### Recent Wiki Links" in text
    assert "[[User Profile]] -working_on-> [[NanoBot Project]]" in text
