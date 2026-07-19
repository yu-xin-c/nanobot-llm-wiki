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


def test_upsert_allocates_distinct_ids_for_slug_collisions(tmp_path) -> None:
    store = WikiStore(tmp_path)

    first = store.upsert_page(title="Alpha Beta", content="First page.")
    second = store.upsert_page(title="Alpha-Beta", content="Second page.")

    assert first.id == "alpha-beta"
    assert second.id.startswith("alpha-beta-")
    assert second.id != first.id
    assert store.status()["pages"] == 2
    assert store.get_page("alpha-beta").content == "First page."
    assert store.get_page("Alpha-Beta").content == "Second page."


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

    _, _, removed = store.unlink_pages("User Profile", "NanoBot Project", "working_on")
    assert removed == 1
    assert store.list_links() == []
    assert store.links_path.read_text(encoding="utf-8") == ""


def test_links_survive_database_rebuild_from_portable_source(tmp_path) -> None:
    store = WikiStore(tmp_path)
    store.upsert_page(title="Source Page", content="Source.")
    store.upsert_page(title="Target Page", content="Target.")
    store.link_pages("Source Page", "Target Page", "supports")

    source_record = json.loads(store.links_path.read_text(encoding="utf-8"))
    assert source_record["from_id"] == "source-page"
    assert source_record["to_id"] == "target-page"

    store.db_path.unlink()
    rebuilt = WikiStore(tmp_path)
    result = rebuilt.reindex_from_disk()

    assert result["links_indexed"] == 1
    assert result["links_skipped"] == []
    assert [(link.from_id, link.to_id, link.relation) for link in rebuilt.list_links()] == [
        ("source-page", "target-page", "supports")
    ]


def test_existing_database_links_are_migrated_when_source_is_missing(tmp_path) -> None:
    store = WikiStore(tmp_path)
    store.upsert_page(title="Old Source", content="Source.")
    store.upsert_page(title="Old Target", content="Target.")
    store.link_pages("Old Source", "Old Target", "precedes")
    store.links_path.unlink()

    migrated = WikiStore(tmp_path)
    record = json.loads(migrated.links_path.read_text(encoding="utf-8"))

    assert record["from_id"] == "old-source"
    assert record["to_id"] == "old-target"
    assert record["relation"] == "precedes"


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


def test_import_knowledge_base_creates_pages_index_and_links(tmp_path) -> None:
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "README.md").write_text("# Product Handbook\n\nUnique token: alpha-import-42.", encoding="utf-8")
    (kb / "notes.txt").write_text("Support escalation policy lives here.", encoding="utf-8")
    (kb / "image.png").write_bytes(b"not imported")

    store = WikiStore(tmp_path / "workspace")
    result = store.import_knowledge_base(kb, index_title="Team Knowledge", tags=["team"])

    assert result.index_page.title == "Team Knowledge"
    assert result.raw_path == str(kb.resolve())
    assert result.source_path == result.raw_path
    assert {item.page.title for item in result.imported} == {"Product Handbook", "notes"}
    assert result.skipped == ["image.png: unsupported extension .png"]
    assert store.search("alpha-import-42")[0].page.title == "Product Handbook"
    assert store.search("Support escalation")[0].page.title == "notes"
    assert "Raw path: `README.md`" in store.get_page("README.md").content

    links = store.list_links()
    assert len(links) == 2
    assert {link.relation for link in links} == {"contains"}
    assert {link.from_title for link in links} == {"Team Knowledge"}

    memory_text = (tmp_path / "workspace" / "memory" / "MEMORY.md").read_text(encoding="utf-8")
    assert "[[Team Knowledge]] -contains-> [[Product Handbook]]" in memory_text
    assert "Raw root:" in result.index_page.content

    (kb / "notes.txt").write_text("Support escalation policy changed.", encoding="utf-8")
    second = store.import_knowledge_base(kb, index_title="Team Knowledge", tags=["team"])
    assert len(second.imported) == 2
    assert store.status()["pages"] == 3
    assert len(store.list_links()) == 2
    assert "changed" in store.get_page("notes.txt").content


def test_import_ids_include_source_root_to_avoid_collisions(tmp_path) -> None:
    first = tmp_path / "first" / "docs"
    second = tmp_path / "second" / "docs"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    (first / "guide.md").write_text("# First Guide\n\nUnique token: first-root.", encoding="utf-8")
    (second / "guide.md").write_text("# Second Guide\n\nUnique token: second-root.", encoding="utf-8")

    store = WikiStore(tmp_path / "workspace")
    first_result = store.import_knowledge_base(first, index_title="First Docs")
    second_result = store.import_knowledge_base(second, index_title="Second Docs")

    assert first_result.index_page.id != second_result.index_page.id
    assert first_result.imported[0].page.id != second_result.imported[0].page.id
    assert store.status()["pages"] == 4
    assert store.search("first-root")[0].page.title == "First Guide"
    assert store.search("second-root")[0].page.title == "Second Guide"


def test_reindex_from_disk_reflects_manual_markdown_edits(tmp_path) -> None:
    store = WikiStore(tmp_path)
    page = store.upsert_page(
        title="Editable Page",
        content="Original indexed content.",
        tags=["editable"],
    )
    page_path = store.page_path(page.id)
    page_path.write_text(
        page_path.read_text(encoding="utf-8").replace(
            "Original indexed content.",
            "Manual edit token reindex-visible.",
        ),
        encoding="utf-8",
    )
    manual_path = store.pages_dir / "manual-note.md"
    manual_path.write_text("# Manual Note\n\nCreated directly as Markdown.", encoding="utf-8")

    result = store.reindex_from_disk()

    assert result["indexed"] == 2
    assert store.search("reindex-visible")[0].page.title == "Editable Page"
    assert store.search("directly as Markdown")[0].page.id == "manual-note"

    manual_path.unlink()
    removed = store.reindex_from_disk()
    assert removed["removed_ids"] == ["manual-note"]
    assert store.get_page("Manual Note") is None
